import argparse
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError

from harness.config import Settings
from harness.core.prompts import birth_record
from harness.mattermost.client import Client, MattermostError
from harness.mattermost.provisioning import provision_seed
from harness.mattermost.smoke import OwnerPostGate, listen
from harness.persistence.bootstrap import load, locked, save

STATE = Path(".state/workspace.json")


def binding(settings: Settings) -> dict[str, str]:
    return {
        "mattermost_url": str(settings.mattermost_url).rstrip("/"),
        "server_id": settings.mattermost_server_id,
        "owner_id": settings.mattermost_owner_id,
    }


async def create(
    settings: Settings,
    path: Path = STATE,
    *,
    inference_enabled: bool = False,
    coordination_enabled: bool = False,
) -> dict[str, Any]:
    if not settings.mattermost_admin_token.get_secret_value() or not settings.mattermost_owner_id:
        raise ValueError("set HARNESS_MATTERMOST_ADMIN_TOKEN and HARNESS_MATTERMOST_OWNER_ID")
    if await asyncio.to_thread(path.exists):
        state = load(path)
        if state["binding"] != binding(settings):
            raise ValueError("checkpoint belongs to a different server or owner")
        if state["state"] == "stopped":
            raise ValueError(
                "workspace is stopped; archive the checkpoint before starting a new workspace"
            )
    else:
        state = {
            "workspace_id": str(uuid4()),
            "seed_id": str(uuid4()),
            "state": "provisioning",
            "created_ms": int(time.time() * 1000),
            "binding": binding(settings),
        }
        # Save IDs before external mutation, allowing safe retry after interruption.
        save(path, state)
    if coordination_enabled and not state.get("bot_username"):
        from harness.mattermost.provisioning import available_name
        from harness.persistence.store import Store

        name_store = await Store.connect(settings)
        try:
            async with Client(
                str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
            ) as name_admin:
                state["bot_username"] = await available_name(
                    name_store,
                    name_admin,
                    settings.mattermost_server_id,
                    UUID(state["seed_id"]),
                    "seed",
                )
        finally:
            await name_store.close()
        save(path, state)
    async with Client(
        str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
    ) as admin:
        info = await provision_seed(
            admin,
            UUID(state["workspace_id"]),
            UUID(state["seed_id"]),
            settings.mattermost_owner_id,
            state.get("bot_token", ""),
            workspace_name=settings.workspace_name
            if coordination_enabled and not state.get("team_name")
            else "",
            team_name=state.get("team_name", ""),
            bot_username=state.get("bot_username", ""),
            existing_bot_id=state.get("bot_user_id", ""),
            channel_name=settings.coordination_channel_name if coordination_enabled else "general",
        )
    async with Client(str(settings.mattermost_url), info["bot_token"]) as bot:
        me = await bot.me()
        if me["id"] != info["bot_user_id"]:
            raise ValueError("seed token is bound to the wrong identity")
    if state.get("general_id") and state["general_id"] != info["general_id"]:
        state["previous_general_id"] = state["general_id"]
    state.update(info)
    # Re-running setup never re-renders an existing prompt.
    if "identity" not in state:
        state["identity"] = birth_record(
            agent_id=UUID(state["seed_id"]),
            workspace_id=UUID(state["workspace_id"]),
            username=info["bot_username"],
            mm_user_id=info["bot_user_id"],
            settings=settings,
            inference_enabled=inference_enabled,
            coordination_enabled=coordination_enabled,
        ).model_dump(mode="json")
    state.update(state="ready", general_subscription="all")
    save(path, state)
    return state


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: state[key]
        for key in (
            "workspace_id",
            "seed_id",
            "state",
            "team_id",
            "general_id",
            "bot_user_id",
            "bot_username",
        )
        if key in state
    }


async def run(command: str, settings: Settings) -> None:
    if command in {"start", "stop", "inspect"}:
        await run_runtime_command(command, settings)
        return
    with locked(STATE):
        if command == "create":
            state = await create(settings)
            print(json.dumps(public_state(state), indent=2))
            print(
                f"Open {str(settings.mattermost_url).rstrip('/')}"
                f"/{state['team_name']}/channels/"
                f"{state.get('coordination_channel_name', 'general')}"
            )
            return
        state = load(STATE)
        if state["binding"] != binding(settings):
            raise ValueError("checkpoint belongs to a different server or owner")
        if command == "inspect":
            print(json.dumps(public_state(state), indent=2))
        elif command == "stop":
            state["state"] = "stopped"
            save(STATE, state)
            async with Client(
                str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
            ) as admin:
                await admin.request("POST", f"bots/{state['bot_user_id']}/disable")
            print("Stopped workspace; Mattermost history retained.")
        elif command == "listen":
            if state["state"] != "ready":
                raise ValueError("workspace must be ready before listening")
            async with Client(str(settings.mattermost_url), state["bot_token"]) as bot:
                await listen(bot, OwnerPostGate(settings.mattermost_owner_id, state["general_id"]))


async def run_runtime_command(command: str, settings: Settings) -> None:
    from harness.core.presentation import configure_workspace
    from harness.core.supervisor import Supervisor
    from harness.models.router import ProviderRouter
    from harness.persistence.store import Store

    if command == "start":
        with locked(STATE):
            if await asyncio.to_thread(STATE.exists) and "reset" in load(STATE):
                from harness.core.reset import finish_reset

                checkpoint = load(STATE)
                if checkpoint["binding"] != binding(settings):
                    raise ValueError("checkpoint belongs to a different server or owner")
                recovery_store = await Store.connect(settings)
                try:
                    await finish_reset(recovery_store, settings, STATE)
                finally:
                    await recovery_store.close()
            state = await create(settings, inference_enabled=True, coordination_enabled=True)
            if "M1 communication proof has no model-callable" in state["identity"]["system_prompt"]:
                raise ValueError(
                    "M1 seed prompt disables inference and is immutable. Stop/archive "
                    "that workspace before creating the M2 workspace."
                )
            if (
                "Spawning and custom tools are not installed yet."
                in state["identity"]["system_prompt"]
            ):
                raise ValueError(
                    "M2 birth prompt disables spawning. Preserve/archive this workspace "
                    "and create an M3 workspace with the current shared template."
                )
            from harness.core.diagnostics import Diagnostics

            diagnostics = Diagnostics(settings, state["team_id"], (state["bot_token"],))
            await diagnostics.start()
            store = None
            provider = None
            try:
                store = await Store.connect(settings)
                provider = ProviderRouter(settings)
                await provider.verify_model()
                await store.initialize(state, settings, state["created_ms"])
                await configure_workspace(store, settings, state, STATE)
                print(
                    f"Workspace URL: {str(settings.mattermost_url).rstrip('/')}"
                    f"/{state['team_name']}/channels/"
                    f"{state.get('coordination_channel_name', 'general')}",
                    flush=True,
                )
                logging.getLogger(__name__).info(
                    "Harness started; workspace %s", state["workspace_id"]
                )
                await Supervisor(
                    store, settings, provider, UUID(state["workspace_id"]), state["bot_token"]
                ).run()
            except Exception:
                logging.getLogger(__name__).exception("Harness startup or supervisor failed")
                raise
            finally:
                logging.getLogger(__name__).info("Harness supervisor stopped; history retained.")
                if provider:
                    await provider.close()
                if store:
                    await store.close()
                await diagnostics.shutdown()
        return
    # Owner controls must remain usable while the supervisor holds its bootstrap lock.
    state = load(STATE)
    if state["binding"] != binding(settings):
        raise ValueError("checkpoint belongs to a different server or owner")
    store = await Store.connect(settings)
    try:
        if command == "stop":
            await store.stop(UUID(state["workspace_id"]))
            state["state"] = "stopped"
            save(STATE, state)
            async with Client(
                str(settings.mattermost_url), settings.mattermost_admin_token.get_secret_value()
            ) as admin:
                await admin.request("POST", f"bots/{state['bot_user_id']}/disable")
            print("Stopped runtime; audit, spend and Mattermost history retained.")
        else:
            a = await store.state(UUID(state["seed_id"]))
            print(
                json.dumps(
                    {
                        "agent_id": str(a["id"]),
                        "state": a["state"],
                        "availability": a["availability"],
                        "spent_usd": str(a["spent"]),
                        "reserved_usd": str(a["reserved"]),
                        "unread": await store.unread_count(a["id"]),
                    },
                    indent=2,
                )
            )
    finally:
        await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Company local Mattermost runtime")
    parser.add_argument(
        "command",
        choices=["create", "listen", "inspect", "stop", "start"],
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args.command, Settings()))
    except KeyboardInterrupt:
        print("Listener stopped; workspace and chat history retained.")
    except ValidationError:
        parser.exit(
            1, "Invalid configuration. Check required URLs, credentials and positive limits.\n"
        )
    except (ValueError, RuntimeError, FileNotFoundError, MattermostError) as error:
        parser.exit(1, f"{error}\n")
    except httpx.HTTPError:
        parser.exit(1, "Mattermost connection failed. Check URL and service health.\n")


if __name__ == "__main__":
    main()
