"""Owner CLI for the same Compose worker locally or on an Agent37 VM."""

import argparse
import asyncio
import base64
import io
import json
import secrets
import tarfile
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx

from harness.coding.clients import Agent37, CodingError, OpenCode, Worker, check
from harness.config import Settings
from harness.persistence.bootstrap import load, locked, save

STATE = Path(".state/coding.json")
INFRA = Path(__file__).resolve().parents[2] / "infra" / "coding"


def update_env(values: dict[str, str]) -> None:
    path = Path(".env")
    lines = path.read_text().splitlines() if path.exists() else []
    keys = set(values)
    lines = [line for line in lines if line.partition("=")[0] not in keys]
    path.write_text("\n".join(lines + [k + "=" + v for k, v in values.items()]) + "\n")
    path.chmod(0o600)


def worker_config(settings: Settings) -> dict[str, Any]:
    models: dict[str, dict[str, Any]] = {}
    for qualified, rates in settings.model_profiles.items():
        provider, _, model = qualified.partition(":")
        if not model:
            continue
        models.setdefault(provider, {})[model] = {
            "name": model,
            "tool_call": True,
            "cost": {"input": float(rates["input"]), "output": float(rates["output"])},
        }
    models.setdefault("openai", {}).setdefault(
        settings.openai_model_id,
        {
            "name": settings.openai_model_id,
            "tool_call": True,
            "cost": {
                "input": float(settings.input_usd_per_million),
                "output": float(settings.output_usd_per_million),
            },
            # This configured model may not be in OpenCode's public catalog. A provider rejection
            # remains an explicit error; discovery is not evidence of a successful inference.
        },
    )
    providers = {}
    for name, url, key in [
        ("openai", settings.openai_base_url, "OPENAI_API_KEY"),
        ("anthropic", settings.anthropic_base_url, "ANTHROPIC_API_KEY"),
        ("deepseek", settings.deepseek_base_url, "DEEPSEEK_API_KEY"),
    ]:
        providers[name] = {
            "options": {"baseURL": str(url).rstrip("/"), "apiKey": "{env:" + key + "}"},
            "models": models.get(name, {}),
        }
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": providers,
        "enabled_providers": list(providers),
        "share": "disabled",
        "autoupdate": False,
        "permission": {
            "*": "allow",
            "task": "deny",
            "external_directory": "deny",
            "webfetch": "deny",
        },
        "agent": {
            "explore": {"disable": True},
            "general": {"disable": True},
            "swarm": {
                "mode": "primary",
                "description": "Tracked Swarm worker",
                # OpenCode merges these into provider-specific SDK options.
                # OpenAI supports this field; other adapters ignore it.
                "options": {"reasoningSummary": "auto"},
            },
        },
    }


async def wait_url(url: str, headers: dict[str, str] | None = None) -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        for _ in range(120):
            try:
                r = await client.get(url, headers=headers)
                if r.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(1)
    raise CodingError("Coding service did not become ready")


async def up(settings: Settings, cloud: bool) -> None:
    if cloud and not settings.agent37_api_key.get_secret_value():
        raise CodingError("Add AGENT37_API_KEY to .env, then run agent-company-coding cloud-up")
    state_path = Path(".state/agent37-coding.json") if cloud else STATE
    with locked(state_path):
        state: dict[str, Any] = (
            load(state_path)
            if await asyncio.to_thread(state_path.exists)
            else {
                "backend": "agent37" if cloud else "docker",
                "password": secrets.token_urlsafe(32),
                "admin_password": secrets.token_urlsafe(32),
                "service_password": secrets.token_urlsafe(32),
            }
        )
        if state["backend"] != ("agent37" if cloud else "docker"):
            raise CodingError(
                "Existing coding installation uses another backend; preserve it before migrating"
            )
        save(state_path, state)
        if cloud:
            api = Agent37(settings)
            if not state.get("instance_id"):
                if state.get("creating_instance"):
                    raise CodingError(
                        "Previous Agent37 create outcome is unknown. Inspect your Agent37 "
                        "instances and record its instance_id in .state/agent37-coding.json "
                        "before retrying; no second billable node was created."
                    )
                state["creating_instance"] = True
                save(state_path, state)
                instance = await api.request(
                    "POST",
                    "instances",
                    json={
                        "template": "agent37-ubuntu",
                        "type": "vm",
                        "name": "My Swarm coding",
                        "resources": {"cpu": 2, "memory": 4, "disk": 12},
                        "auto_sleep": False,
                        "metadata": {"application": "swarm-harness"},
                    },
                )
                state["instance_id"] = instance["id"]
                save(state_path, state)
            settings = settings.model_copy(
                update={"coding_backend": "agent37", "agent37_instance_id": state["instance_id"]}
            )
            if not state.get("gitea_url"):
                port = await api.request(
                    "POST",
                    f"instances/{state['instance_id']}/public-ports",
                    json={"port": 3000, "prefix": "gitea"},
                )
                state["gitea_url"] = port["url"]
                save(state_path, state)
            state["opencode_url"] = f"https://{state['instance_id']}-4096.agent37.app"
        else:
            settings = settings.model_copy(
                update={"coding_backend": "docker", "agent37_instance_id": ""}
            )
            state["gitea_url"], state["opencode_url"] = (
                "http://127.0.0.1:3000",
                "http://127.0.0.1:4096",
            )
        save(state_path, state)
        from harness.coding.tool_source import tool_source
        from harness.tools.core import STREAM_POST_TOOL, TOOLS

        bridge_key = settings.swarm_tool_key.get_secret_value() or secrets.token_urlsafe(32)
        env = {
            "SWARM_TOOL_URL": str(settings.swarm_tool_url).rstrip("/"),
            "SWARM_TOOL_KEY": bridge_key,
            "SWARM_TOOL_DEFINITIONS": json.dumps(
                {
                    t.name: tool_source(t.name, t.description, t.input_schema)
                    for t in TOOLS + [STREAM_POST_TOOL]
                }
            ),
            "HARNESS_OPENCODE_PASSWORD": state["password"],
            "HARNESS_GITEA_TOKEN": state.get("token", "bootstrap-pending"),
            "HARNESS_GITEA_OWNER": settings.gitea_owner,
            "HARNESS_GITEA_REPO": settings.gitea_repo,
            "HARNESS_GITEA_WORKER_URL": str(settings.gitea_worker_url).rstrip("/"),
            "SWARM_OPENCODE_CONFIG": json.dumps(worker_config(settings), separators=(",", ":")),
            "GITEA_ROOT_URL": state["gitea_url"] + "/",
            "CODING_BIND_HOST": "0.0.0.0" if cloud else "127.0.0.1",
            "OPENAI_API_KEY": settings.openai_api_key.get_secret_value(),
            "ANTHROPIC_API_KEY": settings.anthropic_api_key.get_secret_value(),
            "DEEPSEEK_API_KEY": settings.deepseek_api_key.get_secret_value(),
        }
        env_data = "\n".join(k + "='" + v.replace("'", "\\'") + "'" for k, v in env.items()) + "\n"
        env_path = Path(".state/agent37-coding.env") if cloud else Path(".state/coding.env")
        await asyncio.to_thread(env_path.write_text, env_data)
        await asyncio.to_thread(env_path.chmod, 0o600)
        worker = Worker(settings)
        if cloud:
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w:gz") as tar:
                for name in [
                    "Dockerfile",
                    "compose.yaml",
                    "workspace.py",
                    "git-credential.py",
                    "cancel.py",
                    "entrypoint.sh",
                ]:
                    tar.add(INFRA / name, arcname=name)
                entry = tarfile.TarInfo("coding.env")
                entry.mode = 0o600
                entry.size = len(env_data.encode())
                tar.addfile(entry, io.BytesIO(env_data.encode()))
            payload = base64.b64encode(archive.getvalue()).decode()
            await Agent37(settings).execute(
                "mkdir -p /root/swarm-coding && chmod 700 /root/swarm-coding && "
                "printf %s " + payload + " | base64 -d | tar xz -C /root/swarm-coding"
            )
            compose = [
                "docker",
                "compose",
                "--project-directory",
                "/root/swarm-coding",
                "--env-file",
                "/root/swarm-coding/coding.env",
                "-f",
                "/root/swarm-coding/compose.yaml",
            ]
        else:
            compose = [
                "docker",
                "compose",
                "--env-file",
                str(Path.cwd() / env_path),
                "-f",
                str(INFRA / "compose.yaml"),
            ]
        print("Starting Gitea (existing Mattermost and agents are preserved)...", flush=True)
        await worker.command(compose + ["up", "-d", "gitea"])
        await wait_url(state["gitea_url"] + "/user/login")
        if not state.get("admin_created"):
            await worker.command(
                compose
                + [
                    "exec",
                    "-T",
                    "--user",
                    "git",
                    "gitea",
                    "gitea",
                    "--config",
                    "/data/gitea/conf/app.ini",
                    "admin",
                    "user",
                    "create",
                    "--username",
                    "swarm-owner",
                    "--password",
                    state["admin_password"],
                    "--email",
                    "owner@swarm.invalid",
                    "--admin",
                    "--must-change-password=false",
                ]
            )
            state["admin_created"] = True
            save(state_path, state)
        async with httpx.AsyncClient(
            base_url=state["gitea_url"] + "/api/v1/", timeout=20
        ) as client:
            auth = ("swarm-owner", state["admin_password"])
            user = await client.get("users/" + settings.gitea_owner, auth=auth)
            if user.status_code == 404:
                check(
                    await client.post(
                        "admin/users",
                        auth=auth,
                        json={
                            "username": settings.gitea_owner,
                            "password": state["service_password"],
                            "email": "code@swarm.invalid",
                            "must_change_password": False,
                            "send_notify": False,
                        },
                    ),
                    "Gitea",
                )
            required_scopes = ["write:repository", "write:issue", "read:user"]
            if not state.get("token") or state.get("token_scopes") != required_scopes:
                token = check(
                    await client.post(
                        f"users/{settings.gitea_owner}/tokens",
                        auth=(settings.gitea_owner, state["service_password"]),
                        json={
                            "name": "swarm-worker-" + secrets.token_hex(4),
                            "scopes": required_scopes,
                        },
                    ),
                    "Gitea",
                )
                state["token"] = token["sha1"]
                state["token_scopes"] = required_scopes
                save(state_path, state)
            headers = {"Authorization": "token " + state["token"]}
            repo = await client.get(
                f"repos/{settings.gitea_owner}/{settings.gitea_repo}", headers=headers
            )
            if repo.status_code == 404:
                check(
                    await client.post(
                        "user/repos",
                        auth=(settings.gitea_owner, state["service_password"]),
                        json={
                            "name": settings.gitea_repo,
                            "private": True,
                            "auto_init": True,
                            "default_branch": "main",
                            "description": "Shared Swarm coding workspace",
                        },
                    ),
                    "Gitea",
                )
            elif repo.is_error:
                check(repo, "Gitea")
        env["HARNESS_GITEA_TOKEN"] = state["token"]
        env_data = "\n".join(k + "='" + v.replace("'", "\\'") + "'" for k, v in env.items()) + "\n"
        await asyncio.to_thread(env_path.write_text, env_data)
        if cloud:
            await worker.command(["tee", "/root/swarm-coding/coding.env"], env_data.encode())
        print("Building and starting OpenCode worker...", flush=True)
        await worker.command(compose + ["up", "-d", "--build", "worker"])
        prompt_bytes = await asyncio.to_thread(
            (INFRA.parents[1] / "prompts/common_agent.md").read_bytes
        )
        update_env(
            {
                "HARNESS_CODING_ENABLED": "true",
                "HARNESS_AGENT_ENGINE": "opencode",
                "HARNESS_SWARM_TOOL_KEY": bridge_key,
                "HARNESS_CODING_BACKEND": state["backend"],
                "HARNESS_OPENCODE_URL": state["opencode_url"],
                "HARNESS_OPENCODE_PASSWORD": state["password"],
                "HARNESS_GITEA_URL": state["gitea_url"],
                "HARNESS_GITEA_TOKEN": state["token"],
                "HARNESS_AGENT37_INSTANCE_ID": state.get("instance_id", ""),
                "HARNESS_OWNER_PROMPT_REVISION": "1.33.0:" + sha256(prompt_bytes).hexdigest(),
            }
        )
        await health(Settings())
        print(
            "Gitea: " + state["gitea_url"] + "/" + settings.gitea_owner + "/" + settings.gitea_repo
        )
        print("Private Gitea owner login: swarm-owner; password is in " + str(state_path))
        print("Unified OpenCode agents configured. Restart the swarm supervisor.")


async def health(settings: Settings) -> None:
    client = OpenCode(settings)
    try:
        for _ in range(120):
            try:
                health = await client.request("GET", "/global/health")
                print("OpenCode ready: " + str(health.get("version", "unknown")), flush=True)
                return
            except (CodingError, httpx.TransportError):
                await asyncio.sleep(1)
        raise CodingError("OpenCode did not become ready")
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision the Swarm coding worker")
    parser.add_argument("command", choices=["up", "cloud-up", "check"])
    args = parser.parse_args()
    try:
        settings = Settings()
        asyncio.run(
            health(settings)
            if args.command == "check"
            else up(settings, args.command == "cloud-up")
        )
    except (CodingError, httpx.HTTPError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
