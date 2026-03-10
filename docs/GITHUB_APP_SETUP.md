# GitHub App Setup

## Required Permissions
Repository permissions:
- Metadata: Read-only
- Issues: Read and write
- Pull requests: Read and write
- Contents: Read and write

Organization permissions:
- Projects: Read and write

Recommended optional repository permissions:
- Checks: Read and write
- Commit statuses: Read and write
- Actions: Read-only

## Auth Flow
1. Sign a JWT with the GitHub App private key.
2. Exchange the JWT for an installation access token.
3. Use the installation token for REST and GraphQL calls.
4. Refresh the installation token before expiry.

## Required Runtime Env
- `GITHUB_APP_ID`
- `GITHUB_APP_PRIVATE_KEY_PATH`
- `GITHUB_APP_INSTALLATION_ID`
- `GITHUB_OWNER`
- `GITHUB_REPO`

## Notes
- Do not use PAT as the main path.
- Do not embed tokens in git remote URLs.
- Keep the private key outside the repo and outside artifacts.
