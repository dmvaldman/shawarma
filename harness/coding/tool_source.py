"""Generate native tools with the existing schema; never expose a generic API proxy."""

import json
from typing import Any


def schema_source(schema: dict[str, Any]) -> str:
    if "enum" in schema:
        return "tool.schema.enum(" + json.dumps(schema["enum"]) + ")"
    kind = schema.get("type", "string")
    if kind == "array":
        expression = "tool.schema.array(" + schema_source(schema.get("items", {})) + ")"
        if "maxItems" in schema:
            expression += ".max(" + str(schema["maxItems"]) + ")"
        return expression
    if kind == "object":
        return "tool.schema.object(" + args_source(schema) + ").strict()"
    expression = {
        "integer": "tool.schema.number().int()",
        "number": "tool.schema.number()",
        "boolean": "tool.schema.boolean()",
        "string": "tool.schema.string()",
    }[kind]
    for field, method in [
        ("minimum", "min"),
        ("maximum", "max"),
        ("minLength", "min"),
        ("maxLength", "max"),
    ]:
        if field in schema:
            expression += "." + method + "(" + str(schema[field]) + ")"
    if "pattern" in schema:
        expression += ".regex(new RegExp('" + schema["pattern"] + "'))"
    return expression


def args_source(schema: dict[str, Any]) -> str:
    required = schema.get("required", [])
    return (
        "{"
        + ",".join(
            json.dumps(name)
            + ":"
            + schema_source(value)
            + ("" if name in required else ".optional()")
            for name, value in schema.get("properties", {}).items()
        )
        + "}"
    )


def tool_source(name: str, description: str, schema: dict[str, Any]) -> str:
    return (
        """import { tool } from "@opencode-ai/plugin";
export default tool({
description: DESCRIPTION,
args: ARGS,
async execute(args, context) {
 const callID = crypto.randomUUID();
 const response = await fetch(process.env.SWARM_TOOL_URL + "/tools", {
  method: "POST", signal: context.abort,
  headers: {"Content-Type":"application/json",
            "Authorization":"Bearer " + process.env.SWARM_TOOL_KEY},
  body: JSON.stringify({session_id:context.sessionID, name:NAME,
                         call_id:callID, arguments:args})
 });
 // Never replay a mutating call after an unknown HTTP outcome.
 if (!response.ok) throw new Error("Swarm tool HTTP " + response.status + "; inspect before retry");
 return JSON.stringify(await response.json());
}
});
""".replace("DESCRIPTION", json.dumps(description))
        .replace("ARGS", args_source(schema))
        .replace("NAME", json.dumps(name))
    )
