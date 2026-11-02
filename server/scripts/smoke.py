"""End-to-end check against a running stack (make up && make smoke). Uses the live model."""

import asyncio
import sys
import uuid

import httpx

BASE_URL = "http://localhost:8000"


def check(label: str, passed: bool, detail: object = "") -> None:
    print(f"{'PASS' if passed else 'FAIL'}  {label}  {detail}")
    if not passed:
        sys.exit(1)


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=150) as client:
        ready = (await client.get("/readyz")).json()
        check("api, PostgreSQL and Redis are ready", ready["ready"], ready)

        status = (await client.get("/system/status")).json()
        check("worker and scheduler are alive", status["worker"] and status["scheduler"], status)
        check("model key is configured", status["model_configured"])

        ping = (await client.post("/system/ping")).json()
        for _ in range(50):
            ping = (await client.get(f"/system/ping/{ping['id']}")).json()
            if ping["status"] == "completed":
                break
            await asyncio.sleep(0.2)
        check("job went outbox -> Redis -> worker -> done", ping["status"] == "completed", ping)

        session_id = uuid.uuid4().hex
        first = await client.post(
            "/chat",
            json={
                "user_id": "smoke",
                "session_id": session_id,
                "message": "I need a plumber in Indiranagar for my kitchen tap.",
            },
        )
        turn = first.json()
        check("live model replied", turn["outcome"] == "done", turn)

        second = await client.post(
            "/chat",
            json={
                "user_id": "smoke",
                "session_id": session_id,
                "message": "Which area and service did I just give you? Answer in a few words.",
            },
        )
        turn = second.json()
        reply = turn["reply"].lower()
        check("conversation state carried over", "indiranagar" in reply, turn["reply"])

        trace = (await client.get(f"/runs/{turn['run_id']}/trace")).json()
        check(
            "trace has tokens and cost",
            trace["model_calls"] >= 1 and trace["input_tokens"] > 0,
            {
                "model_calls": trace["model_calls"],
                "input_tokens": trace["input_tokens"],
                "output_tokens": trace["output_tokens"],
                "estimated_cost_usd": trace["estimated_cost_usd"],
                "events": [event["kind"] for event in trace["events"]],
            },
        )


if __name__ == "__main__":
    asyncio.run(main())
