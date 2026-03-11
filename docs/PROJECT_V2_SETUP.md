# Projects V2 Setup

## Required Fields
`State` as single select:
- Backlog
- Ready
- In Progress
- Human Review
- Rework
- Merging
- Blocked
- Done
- Cancelled

`Plan` as single select:
- Not Started
- Drafted
- Approved
- Changes Requested

Additional fields:
- `Agent Branch` as text
- `Agent PR` as text
- `Last Agent Run` as date

## Dispatch Gate
Dispatch only when:
- `State` is one of `Ready` or `Rework`
- `Plan` is `Approved`
- no active claim exists for the issue

## Runtime Configuration
Current runtime configuration uses the following env vars:
- `GITHUB_PROJECT_ID`
- `GITHUB_PROJECT_STATE_FIELD_ID`
- `GITHUB_PROJECT_STATE_OPTION_IDS`
- `GITHUB_PROJECT_PLAN_FIELD_ID`
- `GITHUB_PROJECT_PLAN_OPTION_IDS`

`GITHUB_PROJECT_STATE_OPTION_IDS` must be a JSON object, for example:

```json
{
  "Backlog": "PVTSSF_xxx",
  "Ready": "PVTSSF_yyy",
  "In Progress": "PVTSSF_zzz",
  "Human Review": "PVTSSF_aaa",
  "Rework": "PVTSSF_bbb",
  "Blocked": "PVTSSF_ccc",
  "Done": "PVTSSF_ddd",
  "Cancelled": "PVTSSF_eee"
}
```

`GITHUB_PROJECT_PLAN_OPTION_IDS` must be a JSON object, for example:

```json
{
  "Not Started": "PVTSSF_plan_aaa",
  "Drafted": "PVTSSF_plan_bbb",
  "Approved": "PVTSSF_plan_ccc",
  "Changes Requested": "PVTSSF_plan_ddd"
}
```

## Notes
- If you want the bot to operate against a specific Project v2 board, set these `GITHUB_PROJECT_*` variables.
- If these vars are unset, the runtime falls back to state labels.
