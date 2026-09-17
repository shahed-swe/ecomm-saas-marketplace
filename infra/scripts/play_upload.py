#!/usr/bin/env python3
"""Upload one tenant's bundle to that tenant's own Play account.

The tenant owns the listing, the reviews and the install base; we only push a build to it. The
service account JSON belongs to the tenant and is resolved by CI from its own secret store.
"""

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-account", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--track", default="internal")
    parser.add_argument("--aab", required=True)
    args = parser.parse_args()

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        print(
            "google-api-python-client is not installed on this runner; "
            "add it to the workflow before uploading",
            file=sys.stderr,
        )
        return 2

    credentials = service_account.Credentials.from_service_account_info(
        json.load(open(args.service_account)),
        scopes=["https://www.googleapis.com/auth/androidpublisher"],
    )
    service = build("androidpublisher", "v3", credentials=credentials, cache_discovery=False)
    edit = service.edits().insert(body={}, packageName=args.package).execute()
    edit_id = edit["id"]
    uploaded = (
        service.edits()
        .bundles()
        .upload(editId=edit_id, packageName=args.package, media_body=args.aab)
        .execute()
    )
    version_code = uploaded["versionCode"]
    service.edits().tracks().update(
        editId=edit_id,
        track=args.track,
        packageName=args.package,
        body={"releases": [{"versionCodes": [version_code], "status": "completed"}]},
    ).execute()
    service.edits().commit(editId=edit_id, packageName=args.package).execute()
    print(f"uploaded version code {version_code} to {args.track}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
