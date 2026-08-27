#!/usr/bin/env python3
"""Persistent AgentLine relay for Hermes, OpenClaw, Claude Code, and Codex.

Install:
  python agentline_relay.py install --agent-id agt_xxx

The connector auto-detects the local runtime, maintains the AgentLine
WebSocket, maps each phone call to one persistent runtime session, and sends
turn-correlated context back to the caller.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
from pathlib import Path
import plistlib
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import venv


RUNTIMES = ("hermes", "openclaw", "claude-code", "codex", "custom")
STATE_LOCK = threading.Lock()
SESSION_LOCKS: dict[str, asyncio.Lock] = {}


def _home() -> Path:
    return Path(os.environ.get("AGENTLINE_RELAY_HOME", Path.home() / ".agentline"))


def detect_runtime() -> str | None:
    explicit = os.environ.get("AGENTLINE_RUNTIME", "").lower()
    if explicit in RUNTIMES:
        return explicit
    if os.environ.get("HERMES_HOME"):
        return "hermes"
    if os.environ.get("OPENCLAW_HOME"):
        return "openclaw"
    if any(key.startswith("HERMES_") for key in os.environ):
        return "hermes"
    if any(key.startswith("OPENCLAW_") for key in os.environ):
        return "openclaw"
    homes = (
        ("hermes", Path.home() / ".hermes", "hermes"),
        ("openclaw", Path.home() / ".openclaw", "openclaw"),
        ("claude-code", Path.home() / ".claude", "claude"),
        ("codex", Path.home() / ".codex", "codex"),
    )
    for runtime, path, executable in homes:
        if path.exists() and shutil.which(executable):
            return runtime
    for runtime, executable in (
        ("openclaw", "openclaw"),
        ("hermes", "hermes"),
        ("claude-code", "claude"),
        ("codex", "codex"),
    ):
        if shutil.which(executable):
            return runtime
    if os.environ.get("AGENTLINE_HANDLER"):
        return "custom"
    return None


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _ensure_hermes(config: dict) -> None:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    hermes_home.mkdir(parents=True, exist_ok=True)
    env_path = hermes_home / ".env"
    values = _read_env(env_path)
    additions: list[str] = []
    if values.get("API_SERVER_ENABLED", "").lower() != "true":
        additions.append("API_SERVER_ENABLED=true")
    key = values.get("API_SERVER_KEY") or secrets.token_urlsafe(32)
    if not values.get("API_SERVER_KEY"):
        additions.append(f"API_SERVER_KEY={key}")
    port = values.get("API_SERVER_PORT", "8642")
    if additions:
        with env_path.open("a", encoding="utf-8") as handle:
            if env_path.stat().st_size:
                handle.write("\n")
            handle.write("\n".join(additions) + "\n")
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
    config["hermes_url"] = f"http://127.0.0.1:{port}"
    config["hermes_key"] = key
    if config.get("runtime_bin"):
        subprocess.run(_runtime_command(config, "gateway", "install", "--force"), check=True)
        if additions:
            subprocess.run(_runtime_command(config, "gateway", "stop"), check=False)
        subprocess.run(_runtime_command(config, "gateway", "start"), check=True)
    deadline = time.monotonic() + 20
    while True:
        request = urllib.request.Request(
            config["hermes_url"].rstrip("/") + "/v1/capabilities",
            headers={"Authorization": f"Bearer {config['hermes_key']}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=2):
                break
        except (OSError, urllib.error.URLError):
            if time.monotonic() >= deadline:
                raise RuntimeError("Hermes API server did not become ready")
            time.sleep(1)


def _ensure_runtime(runtime: str, config: dict) -> None:
    if runtime == "hermes":
        _ensure_hermes(config)
    elif runtime == "openclaw" and config.get("runtime_bin"):
        subprocess.run(_runtime_command(config, "gateway", "install"), check=False)
        subprocess.run(_runtime_command(config, "gateway", "start"), check=True)
        subprocess.run(
            _runtime_command(config, "gateway", "status", "--require-rpc"),
            check=True,
        )


def _load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_state(config: dict) -> dict:
    path = Path(config["state_file"])
    with STATE_LOCK:
        if not path.exists():
            return {"sessions": {}}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"sessions": {}}


def _set_session(config: dict, session_key: str, runtime_id: str) -> None:
    path = Path(config["state_file"])
    with STATE_LOCK:
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        state.setdefault("sessions", {})[session_key] = runtime_id
        _save_json(path, state)


def _session_id(config: dict, session_key: str) -> str | None:
    return _load_state(config).get("sessions", {}).get(session_key)


def _event_state(config: dict, event_id: str) -> dict | None:
    return _load_state(config).get("events", {}).get(event_id)


def _set_event_state(config: dict, event_id: str, value: dict) -> None:
    path = Path(config["state_file"])
    with STATE_LOCK:
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        events = state.setdefault("events", {})
        events[event_id] = {**value, "updated_at": time.time()}
        if len(events) > 500:
            oldest = sorted(events, key=lambda key: events[key].get("updated_at", 0))
            for key in oldest[:len(events) - 500]:
                events.pop(key, None)
        _save_json(path, state)


def _prompt(event: dict) -> str:
    payload = event.get("payload") or {}
    conversation = payload.get("conversation") or []
    return (
        "You are responding to a human on a live phone call. Work quickly. "
        "Use your normal tools when needed. Return only the final caller-ready "
        "words in one or two concise spoken sentences. AgentLine will speak "
        "your output verbatim, so do not return facts for another model to "
        "rewrite, describe this relay protocol, or return an acknowledgement.\n\n"
        f"Current utterance: {payload.get('utterance', '')}\n"
        f"Conversation: {json.dumps(conversation, ensure_ascii=True)}"
    )


def _generic_event_prompt(event: dict) -> str:
    return (
        "Handle this AgentLine event using your normal tools. Be concise.\n\n"
        + json.dumps(event, ensure_ascii=True)
    )


def _store_inbox_event(config: dict, event: dict) -> None:
    path = Path(config["inbox_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=True) + "\n"
    with STATE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _list_inbox(config: dict) -> list[dict]:
    path = Path(config["inbox_file"])
    state = _load_state(config)
    consumed = set(state.get("inbox_consumed", []))
    events: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_id = event.get("event_id")
            if event_id and event_id not in consumed:
                events[event_id] = event
    return list(events.values())


def _ack_inbox(config: dict, event_ids: list[str]) -> None:
    path = Path(config["state_file"])
    with STATE_LOCK:
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        consumed = list(state.get("inbox_consumed", []))
        for event_id in event_ids:
            if event_id not in consumed:
                consumed.append(event_id)
        state["inbox_consumed"] = consumed[-5000:]
        _save_json(path, state)
        inbox_path = Path(config["inbox_file"])
        if inbox_path.exists():
            acknowledged = set(event_ids)
            retained: list[str] = []
            for line in inbox_path.read_text(encoding="utf-8").splitlines():
                try:
                    event_id = json.loads(line).get("event_id")
                except json.JSONDecodeError:
                    event_id = None
                if event_id not in acknowledged:
                    retained.append(line)
            temporary = inbox_path.with_suffix(inbox_path.suffix + ".tmp")
            temporary.write_text(
                "\n".join(retained) + ("\n" if retained else ""),
                encoding="utf-8",
            )
            temporary.replace(inbox_path)
            try:
                os.chmod(inbox_path, 0o600)
            except OSError:
                pass


def inbox_command(args) -> None:
    config_path = Path(args.config) if args.config else _home() / f"relay-{args.agent_id}.json"
    config = _load_config(config_path)
    if args.action == "list":
        print(json.dumps({"events": _list_inbox(config)}, ensure_ascii=True))
        return
    if not args.event_id:
        raise SystemExit("inbox ack requires at least one --event-id")
    _ack_inbox(config, args.event_id)
    print(json.dumps({"status": "acked", "event_ids": args.event_id}))


RUNTIME_TIMEOUT_SECONDS = 180
RUNTIME_PROCESS_TIMEOUT_SECONDS = 190


def _run(
    command: list[str],
    prompt: str,
    cwd: str,
    timeout: int = RUNTIME_TIMEOUT_SECONDS,
) -> str:
    result = subprocess.run(
        command,
        input=prompt,
        text=True,
        capture_output=True,
        cwd=cwd,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "agent failed")
    return result.stdout.strip()


def _runtime_command(config: dict, *arguments: str) -> list[str]:
    executable = config["runtime_bin"]
    command = [executable, *arguments]
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", *command]
    return command


def _extract_json_text(value: dict) -> str | None:
    for key in ("final", "result", "output_text", "response"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            return text.strip()
    choices = value.get("choices") or []
    if choices:
        text = (choices[-1].get("message") or {}).get("content")
        if isinstance(text, str) and text.strip():
            return text.strip()
    payloads = value.get("payloads") or []
    if payloads and isinstance(payloads[-1], dict):
        text = payloads[-1].get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    output = value.get("output") or []
    for item in reversed(output):
        if item.get("type") == "message":
            parts = []
            for part in item.get("content") or []:
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
            if parts:
                return "".join(parts).strip()
    return None


def _invoke_hermes(config: dict, session_key: str, prompt: str) -> str:
    body = json.dumps({
        "model": "hermes-agent",
        "input": prompt,
        "conversation": session_key,
        "store": True,
    }).encode("utf-8")
    request = urllib.request.Request(
        config["hermes_url"].rstrip("/") + "/v1/responses",
        data=body,
        headers={
            "Authorization": f"Bearer {config['hermes_key']}",
            "Content-Type": "application/json",
            "X-Hermes-Session-Key": session_key[:256],
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=RUNTIME_TIMEOUT_SECONDS) as response:
            value = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RuntimeError(error.read().decode("utf-8", "replace")) from error
    text = _extract_json_text(value)
    if not text:
        raise RuntimeError("Hermes returned no final text")
    return text


def _invoke_openclaw(config: dict, session_key: str, prompt: str) -> str:
    stable_key = "agentline-" + hashlib.sha256(session_key.encode()).hexdigest()[:24]
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as handle:
        handle.write(prompt)
        prompt_path = handle.name
    try:
        output = _run(
            _runtime_command(
                config, "agent", "--session-key", stable_key,
                "--message-file", prompt_path, "--json", "--timeout",
                str(RUNTIME_TIMEOUT_SECONDS),
            ),
            "",
            config["cwd"],
            timeout=RUNTIME_PROCESS_TIMEOUT_SECONDS,
        )
    finally:
        Path(prompt_path).unlink(missing_ok=True)
    value = json.loads(output)
    text = _extract_json_text(value)
    if not text:
        raise RuntimeError("OpenClaw returned no final text")
    return text


def _invoke_claude(config: dict, session_key: str, prompt: str) -> str:
    runtime_id = _session_id(config, session_key)
    arguments = [
        "-p",
        "Answer the live phone request using the event context provided on stdin.",
        "--output-format",
        "json",
    ]
    if runtime_id:
        arguments.extend(["--resume", runtime_id])
    command = _runtime_command(config, *arguments)
    value = json.loads(_run(command, prompt, config["cwd"]))
    new_id = value.get("session_id")
    if new_id:
        _set_session(config, session_key, new_id)
    text = _extract_json_text(value)
    if not text:
        raise RuntimeError("Claude Code returned no final text")
    return text


def _invoke_codex(config: dict, session_key: str, prompt: str) -> str:
    runtime_id = _session_id(config, session_key)
    if runtime_id:
        command = _runtime_command(
            config, "exec", "resume", runtime_id, "--json",
            "--skip-git-repo-check", "-",
        )
    else:
        command = _runtime_command(
            config, "exec", "--json", "--skip-git-repo-check", "-",
        )
    output = _run(command, prompt, config["cwd"])
    text = None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started" and event.get("thread_id"):
            _set_session(config, session_key, event["thread_id"])
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text") or text
    if not text:
        raise RuntimeError("Codex returned no final text")
    return text.strip()


def _invoke_custom(config: dict, session_key: str, prompt: str) -> str:
    handler = config.get("handler") or os.environ.get("AGENTLINE_HANDLER")
    if not handler:
        raise RuntimeError("AGENTLINE_HANDLER is required for runtime=custom")
    env = dict(os.environ)
    env["AGENTLINE_SESSION_KEY"] = session_key
    result = subprocess.run(
        handler,
        input=prompt,
        text=True,
        capture_output=True,
        shell=True,
        cwd=config["cwd"],
        env=env,
        timeout=RUNTIME_TIMEOUT_SECONDS,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(result.stderr.strip() or "custom handler failed")
    return result.stdout.strip()


def invoke(config: dict, session_key: str, prompt: str) -> str:
    runtime = config["runtime"]
    if runtime == "hermes":
        return _invoke_hermes(config, session_key, prompt)
    if runtime == "openclaw":
        return _invoke_openclaw(config, session_key, prompt)
    if runtime == "claude-code":
        return _invoke_claude(config, session_key, prompt)
    if runtime == "codex":
        return _invoke_codex(config, session_key, prompt)
    return _invoke_custom(config, session_key, prompt)


async def _process_event(config: dict, event: dict, outgoing: asyncio.Queue) -> None:
    event_id = event.get("event_id")
    payload = event.get("payload") or {}
    event_type = event.get("event_type")
    session_key = payload.get("session_key") or f"agentline:{event.get('agent_id')}:{payload.get('call_id', event_id)}"
    prompt = _prompt(event) if event_type == "call.utterance" else _generic_event_prompt(event)
    try:
        prior = _event_state(config, event_id)
        if prior and prior.get("status") == "completed" and prior.get("context"):
            text = prior["context"]
        elif (
            prior
            and prior.get("status") == "processing"
            and time.time() - prior.get("updated_at", 0)
            < RUNTIME_PROCESS_TIMEOUT_SECONDS
        ):
            text = None
            deadline = time.monotonic() + RUNTIME_PROCESS_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                current = _event_state(config, event_id)
                if current and current.get("status") == "completed":
                    text = current.get("context")
                    break
            if not text:
                raise RuntimeError("previous processing attempt did not complete")
        else:
            _set_event_state(config, event_id, {"status": "processing"})
            lock = SESSION_LOCKS.setdefault(session_key, asyncio.Lock())
            async with lock:
                text = await asyncio.to_thread(invoke, config, session_key, prompt)
            _set_event_state(config, event_id, {"status": "completed", "context": text})
        if event_type == "call.utterance":
            await outgoing.put({
                "type": "context",
                "call_id": payload.get("call_id"),
                "turn_id": payload.get("turn_id"),
                "push_token": payload.get("push_token"),
                "context": text,
                "event_id": event_id,
            })
        await outgoing.put({"type": "ack", "event_id": event_id})
    except Exception as error:
        _set_event_state(config, event_id, {"status": "failed", "error": str(error)[:500]})
        print(f"relay event {event_id} failed: {error}", file=sys.stderr, flush=True)
        await outgoing.put(None)


async def run_connector(config: dict) -> None:
    import websockets

    base = config["api_base"].rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://"):]
    else:
        # Scheme-less base (e.g. "localhost:8000") — don't slice into the host.
        ws_base = "ws://" + base
    url = f"{ws_base}/v1/events/ws?agent_id={config['agent_id']}&runtime={config['runtime']}"
    delay = 1
    while True:
        try:
            connect_args = {
                "ping_interval": 20,
                "ping_timeout": 20,
                "max_size": 4 * 1024 * 1024,
            }
            header_name = (
                "additional_headers"
                if "additional_headers" in inspect.signature(websockets.connect).parameters
                else "extra_headers"
            )
            connect_args[header_name] = {
                "Authorization": f"Bearer {config['api_key']}"
            }
            async with websockets.connect(url, **connect_args) as socket:
                delay = 1
                outgoing: asyncio.Queue = asyncio.Queue()
                active: dict[str, asyncio.Task] = {}

                async def sender():
                    while True:
                        frame = await outgoing.get()
                        if frame is None:
                            await socket.close(code=1011, reason="runtime event failed; retrying")
                            return
                        await socket.send(json.dumps(frame))

                sender_task = asyncio.create_task(sender())
                try:
                    async for raw in socket:
                        message = json.loads(raw)
                        message_type = message.get("type")
                        if message_type == "heartbeat":
                            await outgoing.put({"type": "ping"})
                        elif message_type in {"acked", "context_result"} and message.get("event_id"):
                            _set_event_state(config, message["event_id"], {"status": "acked"})
                        elif message_type == "event":
                            event_id = message.get("event_id")
                            if message.get("event_type") != "call.utterance":
                                await asyncio.to_thread(_store_inbox_event, config, message)
                                await outgoing.put({"type": "ack", "event_id": event_id})
                                continue
                            task = active.get(event_id)
                            if task is None or task.done():
                                task = asyncio.create_task(_process_event(config, message, outgoing))
                                active[event_id] = task
                                # Drop finished tasks so `active` doesn't grow
                                # unboundedly over a long-lived connection.
                                task.add_done_callback(
                                    lambda _t, eid=event_id: active.pop(eid, None)
                                )
                finally:
                    sender_task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"relay disconnected: {error}; retrying in {delay}s", file=sys.stderr, flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


def _install_relay_environment(relay_home: Path) -> Path:
    environment = relay_home / "venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    environment_ok = False
    if python.is_file():
        check = subprocess.run(
            [str(python), "-I", "-c", "import sys; assert sys.prefix != sys.base_prefix"],
            capture_output=True,
            check=False,
        )
        environment_ok = check.returncode == 0
    if not environment_ok:
        if environment.exists():
            shutil.rmtree(environment)
        venv.EnvBuilder(with_pip=True).create(environment)

    dependency_probe = (
        "import websockets; from importlib.metadata import version; "
        "major = int(version('websockets').split('.', 1)[0]); "
        "raise SystemExit(0 if 12 <= major < 16 else 1)"
    )
    check = subprocess.run(
        [str(python), "-I", "-c", dependency_probe],
        capture_output=True,
        check=False,
    )
    if check.returncode != 0:
        subprocess.check_call([
            str(python), "-m", "pip", "install", "--disable-pip-version-check",
            "websockets>=12,<16",
        ])
    subprocess.check_call([str(python), "-I", "-c", dependency_probe])
    return python


def _install_service(
    config_path: Path,
    agent_id: str,
    script: Path,
    python_executable: Path,
) -> str:
    command = [str(python_executable), str(script), "run", "--config", str(config_path)]
    service_name = "agentline-relay-" + "".join(c for c in agent_id if c.isalnum() or c in "-_")
    if sys.platform.startswith("linux"):
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit = unit_dir / f"{service_name}.service"
        escaped = shlex.join(command)
        unit.write_text(
            "[Unit]\nDescription=AgentLine persistent relay\nAfter=network-online.target\n\n"
            "[Service]\nType=simple\n"
            f"Environment=PATH={shlex.quote(os.environ.get('PATH', ''))}\n"
            "ExecStart=" + escaped + "\nRestart=always\nRestartSec=5\n\n"
            "[Install]\nWantedBy=default.target\n",
            encoding="utf-8",
        )
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", unit.name], check=True)
        return f"systemd user service {unit.name}"
    if sys.platform == "darwin":
        agents = Path.home() / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        label = "cloud.agentline.relay." + hashlib.sha256(agent_id.encode()).hexdigest()[:10]
        plist = agents / f"{label}.plist"
        with plist.open("wb") as handle:
            plistlib.dump({
                "Label": label,
                "ProgramArguments": command,
                "RunAtLoad": True,
                "KeepAlive": True,
                "EnvironmentVariables": {"PATH": os.environ.get("PATH", "")},
                "StandardOutPath": str(_home() / "relay.log"),
                "StandardErrorPath": str(_home() / "relay.log"),
            }, handle)
        subprocess.run(["launchctl", "unload", str(plist)], check=False)
        subprocess.run(["launchctl", "load", str(plist)], check=True)
        return f"launchd service {label}"
    if os.name == "nt":
        task_name = f"AgentLine Relay {agent_id}"
        command_line = subprocess.list2cmdline(command)
        subprocess.run(
            ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", task_name, "/TR", command_line],
            check=True,
        )
        subprocess.run(["schtasks", "/Run", "/TN", task_name], check=True)
        return f"Windows scheduled task {task_name}"
    raise RuntimeError("Unsupported OS; run the printed connector command under your process manager")


def install(args) -> None:
    runtime = args.runtime
    if runtime == "auto":
        runtime = detect_runtime()
    if runtime not in RUNTIMES:
        raise SystemExit("No supported runtime detected. Pass --runtime or set AGENTLINE_HANDLER.")
    api_key = args.api_key or os.environ.get("AGENTLINE_API_KEY")
    if not api_key:
        raise SystemExit("AGENTLINE_API_KEY is required")
    relay_home = _home()
    relay_home.mkdir(parents=True, exist_ok=True)
    relay_python = _install_relay_environment(relay_home)
    config = {
        "version": 1,
        "api_base": args.api_base.rstrip("/"),
        "api_key": api_key,
        "agent_id": args.agent_id,
        "runtime": runtime,
        "cwd": str(Path(args.cwd).resolve()),
        "state_file": str(relay_home / f"sessions-{args.agent_id}.json"),
        "inbox_file": str(relay_home / f"inbox-{args.agent_id}.jsonl"),
        "handler": args.handler or os.environ.get("AGENTLINE_HANDLER"),
    }
    executable_name = {
        "hermes": "hermes",
        "openclaw": "openclaw",
        "claude-code": "claude",
        "codex": "codex",
    }.get(runtime)
    if executable_name:
        runtime_bin = shutil.which(executable_name)
        if not runtime_bin:
            raise SystemExit(f"{executable_name} executable was not found")
        config["runtime_bin"] = str(Path(runtime_bin).resolve())
        if runtime == "claude-code":
            if not any(os.environ.get(key) for key in (
                "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
            )):
                subprocess.run(_runtime_command(config, "auth", "status"), check=True)
        elif runtime == "codex" and not os.environ.get("CODEX_API_KEY"):
            subprocess.run(_runtime_command(config, "login", "status"), check=True)
    _ensure_runtime(runtime, config)
    config_path = relay_home / f"relay-{args.agent_id}.json"
    _save_json(config_path, config)
    installed_script = relay_home / "agentline_relay.py"
    source_script = Path(__file__).resolve()
    if source_script != installed_script.resolve():
        shutil.copy2(source_script, installed_script)
    service = _install_service(
        config_path, args.agent_id, installed_script, relay_python
    )
    print(json.dumps({
        "status": "installed",
        "runtime": runtime,
        "service": service,
        "config": str(config_path),
    }))


def main() -> None:
    parser = argparse.ArgumentParser(description="Persistent AgentLine runtime relay")
    subparsers = parser.add_subparsers(dest="command", required=True)
    detect_parser = subparsers.add_parser("detect")
    detect_parser.set_defaults(func=lambda _args: print(detect_runtime() or "none"))
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--agent-id", required=True)
    install_parser.add_argument("--runtime", choices=("auto",) + RUNTIMES, default="auto")
    install_parser.add_argument("--api-base", default="https://api.agentline.cloud")
    install_parser.add_argument("--api-key")
    install_parser.add_argument("--cwd", default=os.getcwd())
    install_parser.add_argument("--handler")
    install_parser.set_defaults(func=install)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--config", required=True)
    run_parser.set_defaults(func=lambda args: asyncio.run(run_connector(_load_config(Path(args.config)))))
    inbox_parser = subparsers.add_parser("inbox")
    inbox_parser.add_argument("action", choices=("list", "ack"), default="list", nargs="?")
    inbox_target = inbox_parser.add_mutually_exclusive_group(required=True)
    inbox_target.add_argument("--agent-id")
    inbox_target.add_argument("--config")
    inbox_parser.add_argument("--event-id", action="append")
    inbox_parser.set_defaults(func=inbox_command)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
