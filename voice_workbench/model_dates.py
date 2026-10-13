"""Publication dates for an immutable GGUF file revision, fetched in batches."""
from datetime import datetime, timezone
import re


def publication_metadata(value):
    """Reject ambiguous dates; store UTC and preserve the source commit."""
    try:
        date = value.get("published_at")
        parsed = datetime.fromisoformat(date.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("date has no timezone")
        date = parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        commit = value.get("publication_commit")
        commit = commit if isinstance(commit, str) and re.fullmatch(r"[a-f0-9]{40}", commit) else None
        return {"published_at": date, "publication_commit": commit}
    except (AttributeError, ValueError, TypeError, OverflowError):
        return {"published_at": None, "publication_commit": None}


def attach_publication_dates(client, host, repo, revision, files, paths=None, cancel=None):
    """An optional date lookup never prevents verified models from downloading."""
    paths = sorted(set(paths if paths is not None else files) & files.keys())
    for start in range(0, len(paths), 50):
        if cancel is not None and cancel.is_set():
            return
        batch = paths[start:start + 50]
        try:
            response = client.post(f"{host}/api/models/{repo}/paths-info/{revision}",
                                   data={"paths": batch, "expand": "true"})
            response.raise_for_status()
            entries = response.json()
            if not isinstance(entries, list):
                return
            for item in entries:
                if not isinstance(item, dict) or item.get("type") != "file" or item.get("path") not in batch:
                    continue
                commit = item.get("lastCommit") or {}
                files[item["path"]].update(publication_metadata(
                    {"published_at": commit.get("date"), "publication_commit": commit.get("id")}))
        except Exception:
            # Mirrors may lack expanded metadata. Unknown dates remain explicit;
            # do not retry every file or fail the main model discovery request.
            return


def installed_publication(entry, version):
    saved = publication_metadata(version)
    if saved["published_at"]:
        return saved
    if version.get("sha256") and version["sha256"] == entry.get("sha256"):
        return publication_metadata(entry)
    return publication_metadata({})


def retain_publication(entry, previous):
    if not entry.get("published_at") and entry.get("sha256") == previous.get("sha256"):
        return {**entry, **publication_metadata(previous)}
    return entry
