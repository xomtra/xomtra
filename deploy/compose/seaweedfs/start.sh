#!/bin/sh
# Renders the S3 identity config from environment variables (so that keys never live in
# the repository or in the container command line) and starts SeaweedFS.
set -eu
: "${S3_ACCESS_KEY:?}" "${S3_SECRET_KEY:?}"
umask 077
mkdir -p /etc/seaweedfs
cat > /etc/seaweedfs/s3.json <<JSON
{
  "identities": [
    {
      "name": "hammond",
      "credentials": [{"accessKey": "${S3_ACCESS_KEY}", "secretKey": "${S3_SECRET_KEY}"}],
      "actions": ["Admin", "Read", "Write", "List", "Tagging"]
    }
  ]
}
JSON
chown -R seaweed:seaweed /etc/seaweedfs 2>/dev/null || true
exec /entrypoint.sh server \
  -dir=/data \
  -master.volumeSizeLimitMB=1024 \
  -volume.max=0 \
  -s3 \
  -s3.port=8333 \
  -s3.config=/etc/seaweedfs/s3.json
