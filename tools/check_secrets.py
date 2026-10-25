"""Check publishable files, the Git index, or local Git history without printing secrets."""
import argparse
import json
import re
import subprocess
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRECTORIES = {".ssh", ".aws", "secrets", "credentials", "data", "logs"}
PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".db", ".log"}
PATTERNS = (
    ("Telegram bot token", re.compile(r"(?<![\w-])[0-9]{5,}:[A-Za-z0-9_-]{20,}(?![\w-])")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
)
ASSIGNMENT = re.compile(
    r"^[ \t]*(?:export[ \t]+)?TELEGRAM_BOT_TOKEN[ \t]*=[ \t]*([^\r\n]*)", re.MULTILINE
)


def private_path(name):
    path = PurePosixPath(name.replace("\\", "/"))
    basename = path.name.casefold()
    return (
        any(part.casefold() in PRIVATE_DIRECTORIES for part in path.parts[:-1])
        or basename.startswith(".env") and basename != ".env.example"
        or path.suffix.casefold() in PRIVATE_SUFFIXES
        or ".sqlite3" in basename
        or basename.startswith(("id_rsa", "id_ed25519"))
        or ".db-" in basename
    )


def scan_content(body):
    text = body.decode("utf-8", errors="replace")
    findings = set()
    for label, pattern in PATTERNS:
        for match in pattern.finditer(text):
            findings.add((text.count("\n", 0, match.start()) + 1, label))
    for match in ASSIGNMENT.finditer(text):
        value = match.group(1).split("#", 1)[0].strip().strip("\"'")
        if value and not (value.startswith("${") and value.endswith("}")):
            findings.add((text.count("\n", 0, match.start()) + 1, "populated Telegram token setting"))
    return sorted(findings)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.PIPE)


def files(mode):
    if mode == "working":
        names = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
        for raw in names.split(b"\0"):
            if raw:
                name = raw.decode("utf-8", errors="surrogateescape")
                path = ROOT / name
                if path.is_symlink():
                    yield "working", name, b""
                elif path.is_file():
                    yield "working", name, path.read_bytes()
    elif mode == "staged":
        for raw in git("ls-files", "--stage", "-z").split(b"\0"):
            if not raw:
                continue
            header, name = raw.split(b"\t", 1)
            _, object_id, stage = header.split()
            if stage != b"0":
                raise ValueError("Resolve Git conflicts before scanning")
            yield "index", name.decode("utf-8", errors="surrogateescape"), git("cat-file", "blob", object_id.decode())
    else:
        seen = set()
        for commit in git("rev-list", "--all").decode().splitlines():
            for raw in git("ls-tree", "-r", "-z", commit).split(b"\0"):
                if not raw:
                    continue
                header, name = raw.split(b"\t", 1)
                _, object_type, object_id = header.split()
                key = (name, object_id)
                if object_type != b"blob" or key in seen:
                    continue
                seen.add(key)
                yield commit[:12], name.decode("utf-8", errors="surrogateescape"), git("cat-file", "blob", object_id.decode())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true", help="Read the Git index, not working files")
    group.add_argument("--history", action="store_true", help="Read all locally available Git refs")
    args = parser.parse_args(argv)
    mode = "history" if args.history else "staged" if args.staged else "working"
    count = 0
    checked = 0
    try:
        for revision, name, body in files(mode):
            checked += 1
            findings = [(0, "private local file")] if private_path(name) else scan_content(body)
            for line, label in findings:
                # JSON-escape paths so filenames cannot inject terminal/workflow commands.
                print(f"BLOCKED {revision} {json.dumps(name)}:{line}: {label}")
                count += 1
    except (OSError, ValueError, subprocess.CalledProcessError):
        print("Secret check could not finish; verify Git access and local files.")
        return 2
    if count:
        print(f"Secret check failed: {count} findings. Values are intentionally omitted.")
        return 1
    print(f"Secret check passed ({mode}; {checked} files checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
