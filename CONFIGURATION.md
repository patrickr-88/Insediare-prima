# Configuration reference

Two files, both in `config/` on the USB drive:

- **`applications.json`** — the catalogue. This is the file you edit when
  software changes.
- **`settings.json`** — repository-wide policy. Optional; every field has a
  safe default.

A machine-readable JSON Schema is available at
[`docs/schema/applications.schema.json`](docs/schema/applications.schema.json).

Validate after every edit:

```bash
usbinstaller --repository /path/to/drive --validate-config
```

---

## applications.json

```json
{
  "schema_version": 1,
  "applications": [
    {
      "id": "firefox",
      "name": "Mozilla Firefox",
      "version": "141.0",
      "description": "Web browser",
      "category": "Browsers",
      "enabled": true,
      "selected_by_default": true,
      "version_scheme": "numeric",
      "dependencies": [],
      "prerequisites": [],
      "detection": { "method": "windows_registry", "display_name": "^Mozilla Firefox" },

      "windows": {
        "installer": "installers/windows/firefox/FirefoxSetup.exe",
        "type": "exe",
        "arguments": ["/S"],
        "architectures": ["x64"],
        "requires_admin": true
      },

      "macos": {
        "installer": "installers/macos/firefox/Firefox.dmg",
        "type": "dmg",
        "app_bundle": "Firefox.app",
        "detection": { "method": "macos_app_bundle", "bundle": "Firefox.app" }
      }
    }
  ]
}
```

### Application fields

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `id` | string | **required** | Unique. Letters, digits, `-`, `_`, `.` only. Used on the command line. |
| `name` | string | `id` | Display name. |
| `version` | string | `""` | The version *on the drive*. Without it, upgrade detection is disabled. |
| `description` | string | `""` | One line, shown in the GUI. |
| `category` | string | `Uncategorised` | Groups the list. |
| `enabled` | bool | `true` | `false` hides it entirely (keeps the entry for later). |
| `selected_by_default` | bool | `true` | Whether its checkbox starts ticked. |
| `version_scheme` | string | `auto` | `auto`, `semver`, `numeric`, `date`, `string`. |
| `version_pattern` | string | – | Regex; group 1 extracts the comparable part of a messy version. |
| `dependencies` | string[] | `[]` | Other catalogue ids installed **before** this one. |
| `prerequisites` | object[] | `[]` | Machine conditions that must already hold. |
| `detection` | object | `{method:"none"}` | Default detection for all platforms. |
| `windows` / `macos` | object | – | Per-platform payload. **At least one is required.** |

### Platform payload fields

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `installer` | string | **required** | Path **relative to the repository root**. Absolute paths and `..` are rejected. |
| `type` | string | **required** | Windows: `exe`, `msi`, `msix`, `powershell`. macOS: `dmg`, `pkg`, `app`, `shell`. |
| `arguments` | string[] \| string | `[]` | Silent-install switches. A string is split with POSIX rules; the result is always passed as an argv vector (no shell). |
| `architectures` | string[] | all | `x64`, `x86`, `arm64` (aliases `amd64`, `x86_64`, `aarch64`, `apple_silicon`). Filters the machine. |
| `requires_admin` | bool | `true` on Windows and for `pkg` | Drives the privilege check and the `[admin]` marker. |
| `sha256` | string | – | Inline checksum; otherwise `checksums.json` is used. |
| `timeout_seconds` | int | `default_timeout_seconds` | Kills a hung installer. |
| `success_exit_codes` | int[] | `[0]` | e.g. `[0, 3010]` for "installed, reboot required". |
| `detection` | object | inherits | Platform-specific detection, overriding the application-level one. |
| `post_install` | object[] | `[]` | Actions after a successful install. |
| `app_bundle` | string | – | `dmg`/`app`: the `.app` to copy out of the image or archive. |
| `destination` | string | `/Applications` | macOS: where a bundle is copied. |
| `target` | string | `/` | macOS `pkg`: the `installer -target` volume. |

### Version schemes

| Scheme | Compares | Use for |
| --- | --- | --- |
| `auto` | Numbers if present, else equality only | The default; safe for most vendors |
| `semver` | `MAJOR.MINOR.PATCH[-pre]` strictly | Software that really follows semver |
| `numeric` | Every number run, element-wise | `25.01`, `3.0.21-win64`, `24.002.20933` |
| `date` | `YYYY-MM-DD` / `YYYYMMDD` / `YYYY.MM.DD` | Calendar-versioned software |
| `string` | Equality only, never ordering | `"2024 Release"`-style versions |

Anything that cannot be compared is reported as **unknown**, and unknown never
means "safe to overwrite" — the application is skipped with an explanation, and
only `--force` proceeds.

```json
{ "version": "24.002.20933", "version_scheme": "numeric",
  "version_pattern": "Acrobat Reader (\\d[\\d.]*)" }
```

### Detection methods

| Method | Options | Notes |
| --- | --- | --- |
| `none` | – | No signal; always offered, never fails verification. |
| `windows_registry` | `display_name` (regex), `product_code` (MSI GUID), `publisher` (regex) | Reads HKLM, WOW6432Node and HKCU uninstall keys. |
| `windows_path` | `paths[]`, `version_from: "file_version"` | Expands `%ProgramFiles%` and `~`. |
| `macos_app_bundle` | `bundle`, `paths[]`, `version_key` | Reads `Info.plist` (binary or XML). |
| `macos_pkg_receipt` | `package_id` | Runs `pkgutil --pkg-info`. |
| `path_exists` | `paths[]` | Presence only, any platform. |
| `command_version` | `command[]`, `pattern`, `timeout_seconds` | `command[0]` **must** be an absolute path — bare names are refused so nothing is resolved through the target machine's `PATH`. |

Detection runs twice: once before installing (what should we do?) and once
after (did it actually land?). An installer that exits `0` without installing
anything is reported as a failure.

### Prerequisites

Conditions the engine can only *check*, never satisfy:

```json
"prerequisites": [
  { "type": "admin" },
  { "type": "min_os_version", "value": "10.15" },
  { "type": "min_free_disk_mb", "value": 1024, "message": "Needs 1 GB free." },
  { "type": "path_exists", "value": "C:\\Program Files\\Common Files\\Vendor" }
]
```

A failed prerequisite marks the application `[ERROR]` in the plan; the rest of
the run continues.

### Dependencies

```json
{ "id": "application-x", "dependencies": ["dotnet-runtime", "visual-cpp-runtime"] }
```

Dependencies are installed first, transitively, in a deterministic order.
Cycles are detected and named — by the validator before you ship the drive, and
by the planner before anything runs.

### Post-install actions

```json
"post_install": [
  { "type": "script", "script": "scripts/windows/configure-firefox.ps1",
    "arguments": ["-Policy", "corporate"], "timeout_seconds": 120 },
  { "type": "message", "message": "Reboot before using this application." }
]
```

Script paths follow the same containment rules as installers. A failing
post-install action is logged as a warning; it does not turn a successful
installation into a failure.

---

## settings.json

```json
{
  "repository_name": "Workshop Standard Build",
  "strict_checksums": false,
  "block_on_checksum_mismatch": true,
  "warn_on_missing_checksum": true,
  "default_timeout_seconds": 1800,
  "log_retention": 50,
  "log_directory": "logs",
  "minimum_free_disk_mb": 2048,
  "require_confirmation": true,
  "continue_on_failure": true,
  "auto_include_dependencies": true,
  "auto_retry_count": 0,
  "output_capture_bytes": 8192,
  "macos_application_dirs": ["/Applications", "~/Applications"]
}
```

| Setting | Default | Effect |
| --- | --- | --- |
| `strict_checksums` | `false` | A missing **or** mismatched checksum blocks the installer. Recommended for drives that leave the office. |
| `block_on_checksum_mismatch` | `true` | A mismatch blocks even outside strict mode. |
| `warn_on_missing_checksum` | `true` | Missing checksums are warned about. |
| `default_timeout_seconds` | `1800` | Per-installer timeout unless overridden. |
| `log_retention` | `50` | Session directories kept (`0` = keep all). Only this tool's own log directories are ever deleted. |
| `log_directory` | `logs` | Where logs go, relative to the root. |
| `minimum_free_disk_mb` | `2048` | Advisory free-space threshold. |
| `require_confirmation` | `true` | Confirmation before the installation phase (`--yes` overrides). |
| `continue_on_failure` | `true` | Keep installing after a failure. |
| `auto_include_dependencies` | `true` | Pull in dependencies that were not explicitly selected. |
| `auto_retry_count` | `0` | Automatic retries per failed installer. |
| `output_capture_bytes` | `8192` | Installer output kept per application in the log. |
| `macos_application_dirs` | `/Applications`, `~/Applications` | Searched by `macos_app_bundle` detection. |

Unknown keys are ignored and reported as warnings by the validator — a typo in
a security setting must never fail open silently.

---

## What the validator checks

`usbinstaller --validate-config` reports invalid JSON (with line and column),
schema violations, duplicate ids, missing or empty installers, file types that
do not match the declared installer type, unsafe paths, unsupported OS or
architecture values, unknown/disabled dependencies, circular dependencies,
invalid version strings for the declared scheme, missing or contradictory
checksums, unknown prerequisites, missing post-install scripts, and unknown
settings keys.

Add `--verify` to re-hash every installer as well.
