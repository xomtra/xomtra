"""``python -m hacore.secrets_init``: the one-shot service secrets-init (ADR 077)."""

from hacore.secretstore import main

if __name__ == "__main__":
    raise SystemExit(main(["init"]))
