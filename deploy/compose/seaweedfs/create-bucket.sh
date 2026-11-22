#!/bin/sh
# Idempotently creates the lake bucket.
set -eu
: "${S3_BUCKET:?}"
if echo "s3.bucket.list" | weed shell -master=seaweedfs:9333 2>/dev/null | grep -qw "$S3_BUCKET"; then
  echo "bucket $S3_BUCKET exists"
else
  echo "s3.bucket.create -name $S3_BUCKET" | weed shell -master=seaweedfs:9333
  echo "bucket $S3_BUCKET created"
fi
