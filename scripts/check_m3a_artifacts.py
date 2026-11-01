"""Owner-local security check; reports booleans/counts only, never matching text."""
from pathlib import Path
import hashlib
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from autonomous_trader.sandbox.secrets import load_credentials, PreflightError
from autonomous_trader.sandbox.sanitize import Sanitizer, SECRET, normalize


def main():
    credentials = load_credentials(ROOT)
    sanitizer = Sanitizer(credentials.sensitive_values())
    paths = [p for folder in ("src", "tests", "scripts", "docs")
             for p in (ROOT / folder).rglob("*")
             if p.is_file() and p.suffix in (".py", ".json", ".md", ".toml", ".txt", ".xml", ".ps1", ".sql")]
    paths += [ROOT / p for p in ("README.md", "OPEN_DECISIONS.md", "pyproject.toml", "requirements.txt", ".gitignore")]
    for path in paths:
        sanitizer.assert_clean(path.read_text(encoding="utf-8-sig"))
    fixtures = list((ROOT / "tests/fixtures/webull_sandbox").glob("*/*.json"))
    def inspect(value):
        if isinstance(value, dict):
            if any(SECRET.search(normalize(k)) for k in value):
                raise PreflightError("FIXTURE_CREDENTIAL_FIELD_FOUND")
            for item in value.values(): inspect(item)
        elif isinstance(value, list):
            for item in value: inspect(item)
    def inspect_observations(value):
        if isinstance(value, dict):
            if "body" in value:
                inspect(value["body"])
                inspect(value.get("headers", {}))
            for item in value.values(): inspect_observations(item)
        elif isinstance(value, list):
            for item in value: inspect_observations(item)
    for path in fixtures:
        inspect_observations(json.loads(path.read_text()))
    log_files = list(ROOT.glob("webull*_sdk.log*"))
    if log_files or (ROOT / "token.json").exists() or (ROOT / "conf/token.txt").exists():
        raise PreflightError("UNEXPECTED_SDK_LOG_OR_TOKEN_CACHE")
    findings = {
        "artifact_files_scanned": len(paths), "sanitized_fixture_files_checked": len(fixtures),
        "current_known_secret_matches": 0, "credential_fields_in_http_fixture_bodies_or_headers": 0,
        "sdk_log_or_plaintext_token_cache_found": False,
        "configuration_source": "ENVIRONMENT" if any(os.environ.get("WEBULL_SANDBOX_" + k) for k in ("APP_KEY", "APP_SECRET")) else "WINDOWS_DPAPI",
        "account_designation_present": bool(credentials.account_id),
        "access_token_present": bool(credentials.access_token),
        "source_hashes": {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest()
                          for n in ("autonomous-trader-architecture.md", "trade-decision.schema.json")},
        "limits": "Exact current-value scan plus fixture field checks; each earlier probe scanned its then-current credentials before saving. Not a universal secret detector."
    }
    text = json.dumps(findings, indent=2) + "\n"
    sanitizer.assert_clean(text)
    (ROOT / "docs/m3a-security-checks.json").write_text(text, encoding="utf-8")
    print(json.dumps({"security_check": "PASSED", "artifact_files_scanned": len(paths)}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"security_check":"FAILED_DETAILS_SUPPRESSED"}')
        sys.exit(1)
