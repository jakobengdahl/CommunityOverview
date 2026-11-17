"""Manual CLI using the same REST admission boundary as other clients."""

import argparse
import json
import sys

import httpx


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("function_id")
    parser.add_argument("--url", default="http://localhost:8000/api")
    parser.add_argument("--input", default="{}", help="JSON input object")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--correlation-id")
    args = parser.parse_args(argv)
    # netrc supplies HTTP authentication; identity is resolved by the server.
    with httpx.Client(auth=httpx.NetRCAuth(), timeout=150) as client:
        response = client.post(
            f"{args.url.rstrip('/')}/functions/{args.function_id}/invoke",
            json={
                "input": json.loads(args.input),
                "correlation_id": args.correlation_id,
            },
            headers={"Idempotency-Key": args.idempotency_key}
            if args.idempotency_key
            else {},
        )
        response.raise_for_status()
        sys.stdout.write(json.dumps(response.json(), indent=2) + "\n")


if __name__ == "__main__":
    main()
