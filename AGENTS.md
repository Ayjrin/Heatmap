# Agent Working Agreement

This is the standing agreement for any agent working in this repo, on any
feature. It is loaded into every session, so keep it lean and durable — repo
facts and rules that outlast a single task. Task-specific detail belongs in the
prompt, not here.

## Core Directives
1. **Zero Placeholders:** Never leave `// TODO` or `// ... implement later` comments in generated code. Always provide complete, functional implementations.
2. **Strict Type Safety:** Maximize TypeScript and Rust compiler guarantees. Never use `any` in TypeScript. Handle all `Result` and `Option` types explicitly in Rust.
3. **Think Before Coding:** When asked to build a feature or refactor, briefly outline your architectural plan and edge-case considerations before writing the code.


## Quality Assurance & Testing
* Prioritize robust error handling and input validation at all boundaries.
* Write unit tests for business logic and utility functions by default.
* Anticipate failure states (e.g., database timeouts, malformed API payloads) and handle them gracefully.

## UI/UX Standards
* Build modular, reusable components.
* Ensure responsive design using Tailwind CSS utility classes.
* Keep client-side state management strictly scoped to where it is needed.

## Codex Cloud

When running in Codex Cloud, read and follow `AGENTS.cloud.md` before planning
or validating work. It supplements this agreement and overrides conflicting
tool, authentication, and validation instructions for cloud runs.

## Default Behavior

When I ask for an implementation, assume I want you to make the change, validate it, commit it, push it, and open a PR unless I explicitly say not to.

When I ask for a plan, review, explanation, or tradeoff discussion, do not edit files until I approve.

When i ask a question about the codebase, answer it and give me references and line numbers to read the code related to my comment. Make a data flow diagram if necessary. If it is just a question, don't assume that any changes are intended.

Prefer the smallest production-quality change that satisfies the request. Follow existing repo patterns before introducing new abstractions.

When I ask a question about the codebase, answer it and give me references and line numbers to read the code related to my comment. Make a data flow diagram if necessary. If it is just a question, don't assume that any changes are intended.

## Communication

Before editing, briefly state what you are changing and why.

If requirements are ambiguous but a reasonable default exists, proceed and state the assumption. Ask a question only when the ambiguity could cause rework, data loss, security issues, or a materially different product outcome.

Keep final summaries concise:
- what changed
- validation performed
- PR link, if created
- any risks or follow-ups

## Working Method

Work from verified source, not memory. Never invent an API — grep to confirm a function, column, or component exists before you rely on it.

Before editing, do a quick recon: confirm the current behavior in the source and report it against the request's assumptions. Where something is missing or ambiguous, state your assumption and tag it with confidence (High/Medium/Low).

Self-review your own diff before running validation — check RLS/`workspace_id` scoping, dead imports, and framework API misuse. Fix what you find and note the correction.

## Safety

Do not revert unrelated user changes. The working tree is often mutated by parallel agents; stage only the files you touched (never a blanket `git add .`).

Do not run destructive git commands unless explicitly requested.

Do not change database schema, auth behavior, RLS policies, billing, or production integrations without calling it out first. If a task needs a schema/RLS change, propose the migration and pause for approval.

## Validation

Before finishing implementation work, run:

```sh
npm test
npm run trigger:build
npm run lint
npm run build
```

Fix any errors from those commands before committing. (Docs-only changes with no runtime surface don't need the build loop.)

`npm test` is the free tier of the eval suite — pure domain logic, no network, no
model calls. Model-quality evals are separate, cost money, and are run
deliberately rather than on every change: see `docs/evals.md`.

## Git Workflow

Before implementation:
- create a branch from the current branch
- push the branch to origin

After validation:
- stage only the files you changed
- git commit -m "Short imperative summary"
- git push origin HEAD
- open a PR with:
  - concise title
  - summary of changes
  - validation notes

# Project instructions

- Implement AWS infrastructure with Terraform and Docker, as explicitly requested by the user.
- The approved Riot credential store is SSM Parameter Store SecureString. Read .env locally and resolve SSM values only inside the running application; never print secret values or put them in Terraform state. Secrets Manager-specific resolution applies only when using Secrets Manager.
- Serve only verified Riot data. Synthetic inputs belong in isolated temporary tests, never in web/data or deployed assets.
- README.md is the project write-up; do not add a separate take-home document or AI-use examples.

<!-- BEGIN AWS Agent Toolkit rules -->
# AWS Guidance
- Where these AWS rules conflict with the project's own instructions, the
  project's instructions take precedence.
- Prefer the AWS MCP Server for AWS interactions — it provides sandboxed
  execution, observability, and audit logging. If unavailable, use the
  AWS CLI directly.
- Before starting a task, check whether a relevant AWS skill is available.
  Load the skill with `retrieve_skill` and prefer its guidance over
  general knowledge.
- When uncertain about specific AWS details (API parameters, permissions,
  limits, error codes), verify against documentation rather than guessing.
  State uncertainty explicitly if you cannot confirm.
- When creating infrastructure, prefer infrastructure-as-code (AWS CDK or
  CloudFormation) over direct CLI commands.
- When working with infrastructure, follow AWS Well-Architected Framework
  principles.
- Do not use em dashes in AWS resource names or descriptions. Use
  hyphens instead.
## Secret Safety

- MUST load the `aws-secrets-manager` skill first for any secret,
  credential, API key, token, or password task. MUST NOT call
  `secretsmanager get-secret-value` or `batch-get-secret-value`, and MUST
  NOT hit the Secrets Manager Agent daemon directly. MUST use
  `{{resolve:secretsmanager:secret-id:SecretString:json-key}}` with
  `asm-exec` so the secret resolves at runtime without entering context.
<!-- END AWS Agent Toolkit rules -->
