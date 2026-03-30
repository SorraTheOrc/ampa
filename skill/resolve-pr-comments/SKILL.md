---
name: resolve-pr-comments
description: Fetch GitHub PR review comments, propose fixes with a plan, and resolve threads after approval
license: MIT
compatibility: opencode
metadata:
  audience: developers
  workflow: github
---

## What I do

- Fetch code review comments from a GitHub pull request
- Check out the PR branch
- Analyze each code review comment and categorize it
- Present a detailed plan of proposed fixes (or disagreements) for approval
- After approval, implement fixes, commit, push, reply to threads, and resolve them
- For non-code comments (general PR comments, questions), propose responses in chat without taking action

## When to use me

Use this skill when:

- You have a PR with code review comments that need to be addressed
- You want to systematically review and resolve all feedback
- You need to reply to reviewers explaining what was fixed (or why you disagree)

## Required information

Provide one of:

- **PR URL**: e.g., `https://github.com/owner/repo/pull/123`
- **PR number + repository**: e.g., `PR #123 in owner/repo`
- **PR number only**: If already in the correct repository context

---

## Workflow

### Phase 1: Discovery (Read-Only)

#### Step 1.1: Get PR metadata

```bash
gh pr view <PR_NUMBER> --repo <OWNER/REPO> --json headRefName,baseRefName,title,url
```

#### Step 1.2: Fetch all review comments

```bash
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments
```

This returns an array of review comments with:

- `id`: Comment ID for replies
- `body`: The review comment text
- `path`: File path
- `line` / `original_line`: Line number(s)
- `diff_hunk`: Code context
- `user.login`: Reviewer username

#### Step 1.3: Fetch general PR comments (non-code)

```bash
gh api repos/<OWNER>/<REPO>/issues/<PR_NUMBER>/comments
```

#### Step 1.4: Checkout the PR branch

```bash
git fetch origin <branch_name>
git checkout <branch_name>
```

#### Step 1.5: Read referenced code

For each code review comment, read the file and surrounding context:

```bash
# Use Read tool to examine the file at the specified line
```

---

### Phase 2: Analysis & Planning

#### Step 2.1: Categorize each comment

For each **code review comment**, categorize as one of:

1. **Actionable fix**: Clear code change requested
2. **Suggestion to evaluate**: Proposed improvement that needs consideration
3. **Question**: Needs a response but no code change
4. **Disagreement**: The comment suggests a change that would be incorrect or harmful

For each **general PR comment** (non-code):

- These will NOT be actioned automatically
- Propose a response in chat for the user to review

#### Step 2.2: Present the plan

Output a structured plan in this format:

```
## PR Review Resolution Plan

**PR:** #<number> - <title>
**Branch:** <branch_name>
**Comments found:** <X> code review comments, <Y> general comments

---

### Code Review Comments

#### Comment 1: [<file>:<line>] by @<reviewer>
> <quoted comment body>

**Category:** Actionable fix
**Proposed action:** <description of the fix>
**Files to modify:** <list of files>

---

#### Comment 2: [<file>:<line>] by @<reviewer>
> <quoted comment body>

**Category:** Disagreement
**Reasoning:** <explanation of why the suggested change is incorrect>
**Proposed response:** <reply to post explaining the disagreement>

---

### General PR Comments (No automatic action)

#### Comment A: by @<reviewer>
> <quoted comment body>

**Proposed response:** <suggested reply for user to review>

---

## Summary

- **Fixes to implement:** <N>
- **Disagreements to explain:** <N>
- **Questions to answer:** <N>
- **General comments needing response:** <N>

**Ready to proceed?** Reply "yes" to implement fixes and post responses, or provide feedback on specific items.
```

---

### Phase 3: Execution (After Approval)

Only proceed after user confirms the plan.

#### Step 3.1: Implement code fixes

For each approved actionable fix:

1. Use Edit tool to make the change
2. Track which comment IDs were addressed

#### Step 3.2: Commit and push

```bash
git add <modified_files>
git commit -m "Address PR review feedback

- <summary of fix 1>
- <summary of fix 2>
..."
git push origin <branch_name>
```

Capture the commit hash for reference in replies.

#### Step 3.3: Reply to code review comment threads

For each comment addressed (fix or disagreement):

```bash
gh api repos/<OWNER>/<REPO>/pulls/<PR_NUMBER>/comments \
  -X POST \
  -f body="<description of action taken or disagreement explanation>" \
  -F in_reply_to=<COMMENT_ID>
```

**Reply templates:**

For fixes:

```
Fixed in <commit_hash>. <brief description of what was changed>
```

For disagreements:

```
After review, I believe the current implementation is correct because <reasoning>.
<optional: suggest alternative or offer to discuss further>
```

#### Step 3.4: Resolve review threads

First, get thread IDs:

```bash
gh api graphql -f query='
query {
  repository(owner: "<OWNER>", name: "<REPO>") {
    pullRequest(number: <PR_NUMBER>) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 1) {
            nodes {
              databaseId
            }
          }
        }
      }
    }
  }
}'
```

Map comment IDs to thread IDs, then resolve each addressed thread:

```bash
gh api graphql -f query='
mutation {
  resolveReviewThread(input: {threadId: "<THREAD_ID>"}) {
    thread {
      isResolved
    }
  }
}'
```

#### Step 3.5: Report completion

Output a summary:

```
## PR Review Resolution Complete

**Commit:** <hash>
**Fixes applied:** <N>
**Disagreements explained:** <N>
**Threads resolved:** <N>

### Actions taken:
1. [<file>:<line>] - <action taken>
2. [<file>:<line>] - <action taken>
...

### Threads resolved:
- Comment by @<reviewer>: <brief description>
...

### Pending (requires manual action):
- General comment by @<reviewer>: <proposed response provided above>
```

---

## Important guidelines

1. **Never auto-execute**: Always present the plan and wait for approval before making changes
2. **Respect disagreements**: It's valid to disagree with a review comment - explain why clearly and professionally
3. **Group related fixes**: If multiple comments relate to the same issue, address them together
4. **Reference commits**: Always include commit hashes in replies so reviewers can verify
5. **Don't resolve questions**: Only resolve threads where you've taken action or explained a disagreement
6. **Verify push access**: Before making changes, ensure the branch can be pushed to
7. **Handle conflicts**: If the branch has conflicts or is behind, notify the user before proceeding

## Error handling

- **Comment on outdated code**: Note this in the plan; the fix may need adjustment
- **Ambiguous requests**: Ask for clarification in the plan rather than guessing
- **Permission denied**: Report the error and suggest the user check repository access
- **Branch not found**: Verify the PR is still open and the branch exists
