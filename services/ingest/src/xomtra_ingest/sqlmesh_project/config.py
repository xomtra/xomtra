"""SQLMesh project config: built from environment variables at load time.

See ``xomtra_ingest.transform.build_config``; tests pass their own config object.
"""

from xomtra_ingest.transform import config_from_env

config = config_from_env()
