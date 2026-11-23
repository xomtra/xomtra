"""SQLMesh project config: built from environment variables at load time.

See ``hammond_ingest.transform.build_config``; tests pass their own config object.
"""

from hammond_ingest.transform import config_from_env

config = config_from_env()
