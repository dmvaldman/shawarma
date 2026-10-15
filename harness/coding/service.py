"""One persistent OpenCode session per swarm identity; bounded explicit coding steps."""

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from harness.coding.clients import CodingError, CodingInterrupted, OpenCode, Worker
from harness.coding.identities import identity as gitea_identity
from harness.coding.repositories import selected
from harness.config import Settings
from harness.core.types import ModelResponse, Usage
from harness.persistence.store import Store, decode

logger = logging.getLogger(__name__)


def native_error(error: dict[str, Any]) -> str:
    """Expose error classification without leaking provider headers or request content."""
    data = error.get("data") or {}
    status = data.get("statusCode")
    code = "request_failed"
    try:
        body = json.loads(data.get("responseBody") or "{}")
        candidate = body.get("error", {}).get("code")
        if isinstance(candidate, str) and candidate.replace("_", "").isalnum():
            code = candidate[:80]
    except (ValueError, AttributeError, TypeError):
        pass
    if code == "invalid_file":
        return (
            "OpenCode provider rejected a corrupted/unsupported file (invalid_file); "
            "partial work preserved"
        )
    if isinstance(status, int):
        return f"OpenCode provider HTTP {status} ({code}); partial work preserved"
    return "OpenCode coding step failed; partial work preserved"


def summarize(messages: list[dict[str, Any]], baseline: set[str]) -> tuple[str, Usage]:
    text: list[str] = []
    input_tokens = output_tokens = 0
    cost = Decimal(0)
    details = []
    for message in messages:
        info = message["info"]
        if info["id"] in baseline or info["role"] != "assistant":
            continue
        # OpenCode's assistant tokens describe its latest step; step-finish parts
        # preserve every internal model call. Do not count both representations.
        steps = [part for part in message.get("parts", []) if part.get("type") == "step-finish"]
        counters = [step.get("tokens", {}) for step in steps] or [info.get("tokens", {})]
        for tokens in counters:
            cache = tokens.get("cache", {})
            input_tokens += (
                int(tokens.get("input", 0)) + int(cache.get("read", 0)) + int(cache.get("write", 0))
            )
            output_tokens += int(tokens.get("output", 0)) + int(tokens.get("reasoning", 0))
        cost += Decimal(str(info.get("cost", 0)))
        details.append({"id": info["id"], "steps": counters, "cost": info.get("cost")})
        for part in message.get("parts", []):
            if part.get("type") == "text":
                text.append(part.get("text", ""))
    return "\n\n".join(text)[-12000:], Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        provider_usage={
            "source": "opencode",
            "messages": details,
            "cost_basis": "OpenCode model pricing; provider billing remains authoritative",
        },
    )


class Coding:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings

    async def allowed(self, agent: UUID) -> dict[str, Any]:
        state = await self.store.state(agent)
        if (
            state["workspace_state"] != "active"
            or state["control_paused"]
            or state["state"] == "TERMINATED"
        ):
            raise CodingError("Coding work is paused or terminated")
        return state

    async def abort(self, client: OpenCode, session: dict[str, Any]) -> None:
        await client.request(
            "POST", f"/session/{session['session_id']}/abort", session["directory"]
        )
        for _ in range(20):
            statuses = await client.request("GET", "/session/status", session["directory"])
            if statuses.get(session["session_id"], {}).get("type", "idle") == "idle":
                # Session idle can precede native shell cleanup. Reap only this
                # checkout's commands before confirming owner cancellation.
                await Worker(self.settings).stop_commands(session["directory"])
                return
            await asyncio.sleep(0.25)
        raise CodingError("OpenCode abort not confirmed; coding session requires reconciliation")

    async def recover(self) -> None:
        if not self.settings.coding_enabled:
            return
        client = OpenCode(self.settings)
        try:
            rows = await self.store.pool.fetch(
                "SELECT r.*,s.session_id,s.directory FROM coding_runs r "
                "JOIN coding_sessions s USING(agent_id) WHERE r.state='running'"
            )
            for row in rows:
                session = dict(row)
                await self.abort(client, session)
                messages = await client.request(
                    "GET", f"/session/{session['session_id']}/message", session["directory"]
                )
                _, usage = summarize(messages, set(decode(row["baseline"])))
                # Recovery precedes ordinary reservation recovery in the supervisor.
                if row["turn_id"]:
                    await self.store.finish(row["turn_id"], usage)
                await self.store.pool.execute(
                    "UPDATE coding_runs SET state='interrupted',finished_at=now() WHERE id=$1",
                    row["id"],
                )
                logger.warning(
                    "Interrupted coding run %s reconciled; inspect branch before retry", row["id"]
                )
        finally:
            await client.close()

    async def run(self, agent: UUID, call_id: str, task: str) -> dict[str, Any]:
        if not self.settings.coding_enabled:
            raise CodingError("Coding is disabled")
        if not self.settings.opencode_password.get_secret_value():
            raise CodingError("Configure HARNESS_OPENCODE_PASSWORD before enabling coding")
        async with self.store.pool.acquire() as lease:
            # Session advisory lock spans remote I/O without a long-running transaction.
            lock_id = "coding:" + str(agent)
            if not await lease.fetchval(
                "SELECT pg_try_advisory_lock(hashtextextended($1,0))", lock_id
            ):
                raise CodingError("Agent already has a coding operation")
            client = OpenCode(self.settings)
            try:
                async with asyncio.timeout(self.settings.coding_timeout_sec):
                    return await self._run(client, agent, call_id, task)
            finally:
                await client.close()
                await lease.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", lock_id)

    unified = False

    async def session(self, client: OpenCode, agent: UUID, state: dict[str, Any]) -> dict[str, Any]:
        worker_id = (
            self.settings.agent37_instance_id
            if self.settings.coding_backend == "agent37"
            else self.settings.coding_container
        )
        target = await selected(self.store, self.settings, agent)
        account = await gitea_identity(self.store, target, agent)
        workspace = await Worker(self.settings).workspace(
            state["workspace_id"],
            agent,
            {
                "username": account["username"],
                "token": account["token"],
                "email": account["email"],
                "repository": target.gitea_owner + "/" + target.gitea_repo,
            },
        )
        session_row = await self.store.pool.fetchrow(
            "SELECT * FROM coding_sessions WHERE agent_id=$1", agent
        )
        if session_row:
            session = dict(session_row)
            if session["worker_id"] != worker_id:
                raise CodingError("Session belongs to another worker; migrate it explicitly")
        else:
            created = await client.request(
                "POST", "/session", workspace["directory"], json={"title": "Swarm " + str(agent)}
            )
            session = {**workspace, "session_id": created["id"], "worker_id": worker_id}
            await self.store.pool.execute(
                "INSERT INTO coding_sessions(agent_id,session_id,directory,branch,worker_id) "
                "VALUES($1,$2,$3,$4,$5)",
                agent,
                session["session_id"],
                session["directory"],
                session["branch"],
                worker_id,
            )
        return session

    async def prepare_session(self, client: OpenCode, agent: UUID, session: dict[str, Any]) -> None:
        pass

    async def poll_session(self, client: OpenCode, agent: UUID, session: dict[str, Any]) -> None:
        pass

    async def observe_session(
        self, agent: UUID, session: dict[str, Any], messages: list[dict[str, Any]]
    ) -> None:
        pass

    async def system_prompt(self, agent: UUID, session: dict[str, Any]) -> str:
        return (
            "You are the coding workspace of a tracked Swarm agent. "
            "Execute this coding step and report actual changes and test results. "
            "Do not create hidden subagents or access other workspaces. "
            "Keep your assigned branch. Collaborate through Git; commit and push useful "
            "changes only to " + str(session["branch"]) + ". Never push main or merge others' work "
            "without request. Do not read, print, or commit credentials. Communication and "
            "delegation are handled by your Swarm agent. Preserve unfinished work on cancellation."
        )

    async def _run(self, client: OpenCode, agent: UUID, call_id: str, task: str) -> dict[str, Any]:
        state = await self.allowed(agent)
        old = await self.store.pool.fetchrow(
            "SELECT * FROM coding_runs WHERE agent_id=$1 AND call_id=$2", agent, call_id
        )
        if old:
            if old["state"] == "completed":
                return dict(decode(old["result"]))
            raise CodingError("Previous coding operation has uncertain effects; inspect its branch")
        active = await self.store.pool.fetchval(
            "SELECT 1 FROM coding_runs WHERE agent_id=$1 AND state='running'", agent
        )
        if active:
            raise CodingError("Unreconciled coding operation; restart to abort and reconcile it")
        session = await self.session(client, agent, state)
        await self.allowed(agent)
        model = state["model_id"]
        if ":" not in model:
            model = "openai:" + model
        provider, model_id = model.split(":", 1)
        providers = await client.request("GET", "/provider", session["directory"])
        known = next((p for p in providers["all"] if p["id"] == provider), None)
        if provider not in providers["connected"] or not known or model_id not in known["models"]:
            raise CodingError("Exact agent model is unavailable in OpenCode; no model substituted")
        if known["models"][model_id].get("limit", {}).get("context", 0) <= 0:
            raise CodingError("OpenCode lacks context metadata for this model; update its catalog")
        await self.prepare_session(client, agent, session)
        before = await client.request(
            "GET", f"/session/{session['session_id']}/message", session["directory"]
        )
        baseline = {m["info"]["id"] for m in before}
        run_id, prompt_id = uuid4(), "msg_" + uuid4().hex
        turn_id = await self.store.reserve_cost(agent, Decimal(0), [], provider_model_id=model)
        await self.store.pool.execute(
            "INSERT INTO coding_runs(id,agent_id,call_id,turn_id,baseline) VALUES($1,$2,$3,$4,$5)",
            run_id,
            agent,
            call_id,
            turn_id,
            json.dumps(sorted(baseline)),
        )
        settled = False
        try:
            await self.allowed(agent)
            await client.request(
                "POST",
                f"/session/{session['session_id']}/prompt_async",
                session["directory"],
                json={
                    "messageID": prompt_id,
                    "model": {"providerID": provider, "modelID": model_id},
                    "agent": "swarm" if self.unified else "build",
                    "system": await self.system_prompt(agent, session),
                    "parts": [{"type": "text", "text": task}],
                },
            )
            while True:
                await asyncio.sleep(1)
                await self.allowed(agent)
                await self.poll_session(client, agent, session)
                messages = await client.request(
                    "GET", f"/session/{session['session_id']}/message", session["directory"]
                )
                await self.observe_session(agent, session, messages)
                # A native activation may contain many model steps before going idle.
                # Keep its existing ledger row current; finish overwrites, never adds, usage.
                _, observed_usage = summarize(messages, baseline)
                await self.store.observe_usage(turn_id, observed_usage)
                replies = [m for m in messages if m["info"].get("parentID") == prompt_id]
                if self.unified:
                    replies = [
                        m
                        for m in messages
                        if m["info"]["id"] not in baseline and m["info"]["role"] == "assistant"
                    ]
                statuses = await client.request("GET", "/session/status", session["directory"])
                if (
                    replies
                    and statuses.get(session["session_id"], {}).get("type", "idle") == "idle"
                ):
                    report, usage = summarize(messages, baseline)
                    native_response = (
                        ModelResponse(
                            usage=usage,
                            message={
                                "id": replies[-1]["info"]["id"],
                                "output": [
                                    {
                                        "type": "message",
                                        "role": "assistant",
                                        "content": [{"type": "output_text", "text": report}],
                                    }
                                ],
                            },
                        )
                        if self.unified
                        else None
                    )
                    await self.store.finish(turn_id, usage, native_response)
                    settled = True
                    if errors := [m["info"]["error"] for m in replies if m["info"].get("error")]:
                        failures = [e for e in errors if e.get("name") != "MessageAbortedError"]
                        if failures:
                            raise CodingError(native_error(failures[-1]))
                        raise CodingInterrupted(
                            "OpenCode turn interrupted; resuming preserved session"
                        )
                    result = {
                        "run_id": str(run_id),
                        "session_id": session["session_id"],
                        "branch": session["branch"],
                        "report": report,
                        "output_tokens": usage.output_tokens,
                        "cost_usd": str(usage.cost_usd),
                        "cost_basis": "OpenCode estimate",
                    }
                    await self.store.pool.execute(
                        "UPDATE coding_runs SET state='completed',result=$2,finished_at=now() "
                        "WHERE id=$1",
                        run_id,
                        json.dumps(result),
                    )
                    logger.info(
                        "Agent %s completed coding run %s on %s", agent, run_id, session["branch"]
                    )
                    return result
        except BaseException:
            # Cancel remote execution, not just the local HTTP request. Failed aborts leave
            # a durable running record that blocks further work until reconciliation.
            try:
                await self.abort(client, session)
                messages = await client.request(
                    "GET", f"/session/{session['session_id']}/message", session["directory"]
                )
                if not settled:
                    _, usage = summarize(messages, baseline)
                    await self.store.finish(turn_id, usage)
                await self.store.pool.execute(
                    "UPDATE coding_runs SET state='interrupted',finished_at=now() WHERE id=$1",
                    run_id,
                )
            except Exception:
                logger.exception(
                    "Agent %s coding cancellation unconfirmed; blocked until reconciliation",
                    agent,
                )
            raise
