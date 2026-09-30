import asyncio
import json
from pathlib import Path

from aos import supervisor


original_request = supervisor.request_json


def inspect_response(*arguments, **keywords):
    response = original_request(*arguments, **keywords)
    if "choices" in response:
        print(json.dumps({"finish_reason": response["choices"][0].get("finish_reason"),
                          "content": response["choices"][0].get("message", {}).get("content"),
                          "usage": response.get("usage")}), flush=True)
    else:
        print(json.dumps({"route": arguments[2], "response": response}), flush=True)
    return response


async def main():
    supervisor.request_json = inspect_response
    engine = supervisor.BonsaiSupervisor(Path("models/bonsai-manifest.json"))
    result = await engine.plan("Create and verify the authorized /workspace/hello.txt containing Hello from the local agent. followed by a newline. The initial read failed because the file is absent. No overwrite, host access or other files are authorized.",
                               [{"id": "synthetic-missing-file", "kind": "filesystem.read", "summary": "Synthetic protocol probe: hello.txt is absent and read returned ELEMENT_MISSING without side effects."}])
    print(result.model_dump_json())
    print(json.dumps(engine.last_metrics))


if __name__ == "__main__":
    asyncio.run(main())
