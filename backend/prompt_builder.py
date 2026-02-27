from context_builder import ReviewContext, FileReviewContext


def build_prompt(ctx: ReviewContext) -> str:
    prompt = f"""You are a senior software engineer reviewing a PR for this repository.

Your PRIMARY job: check if new code FITS THE EXISTING CODEBASE STRUCTURE AND PATTERNS.
You have been given structurally similar files found via semantic vector search — treat them as the ground truth for "how things are done here".

PR: {ctx.pr_title}
Author: {ctx.pr_author}
Branch: {ctx.base_branch}
URL: {ctx.pr_url}

Description:
{ctx.pr_description or "(no description)"}

---

## REPOSITORY STRUCTURE
```
{ctx.repo_structure}
```

---
"""

    for fc in ctx.files:
        prompt += f"\n## FILE: {fc.changed_file}  |  Status: {fc.status.upper()}  |  Layer: {fc.inferred_layer.upper()}\n\n"

        if fc.similar_files:
            prompt += "### STRUCTURALLY SIMILAR FILES (vector search results — USE AS REFERENCE PATTERNS):\n"
            for i, sf in enumerate(fc.similar_files, 1):
                prompt += f"\n**[{i}] {sf['file_path']} (similarity: {sf['similarity_score']})**\n"
                prompt += f"```\n{sf['content']}\n```\n"

        if fc.directory_siblings:
            prompt += "\n### SIBLING FILES IN SAME DIRECTORY:\n"
            for sib in fc.directory_siblings[:2]:
                prompt += f"\n**{sib['path']}**\n```\n{sib['content']}\n```\n"

        if fc.original_content:
            prompt += f"\n### ORIGINAL FILE (before this PR):\n```\n{fc.original_content}\n```\n"

        prompt += f"\n### PR DIFF:\n```diff\n{fc.diff}\n```\n"

        prompt += """
### REVIEW CHECKLIST FOR THIS FILE:

1. **STRUCTURE MATCH** — Does the new code follow the same structural patterns as similar files?
   (class organization, method ordering, base classes, interfaces implemented)

2. **NAMING CONVENTIONS** — Consistent with existing codebase patterns?

3. **IMPORT PATTERNS** — Same import organization and aliasing style?

4. **ERROR HANDLING** — Same error handling approach as existing similar files?

5. **LAYER VIOLATIONS** — Is business logic, DB access, or HTTP logic in the wrong layer?

6. **MISSING PIECES** — What do similar files have that this new code is missing?
   (missing interface implementation, missing test, missing type export, missing registration)

7. **NEW ANTI-PATTERNS** — Does this introduce a new way of doing something that already has an established pattern?

8. **BUGS / LOGIC ISSUES** — Standard correctness review.

---
"""

    prompt += """
## FINAL SUMMARY

**STRUCTURE VIOLATIONS** (must fix — breaks codebase consistency):
List each with: file, what's wrong, what the correct pattern should be based on similar files

**BUGS / LOGIC ISSUES** (must fix):

**SUGGESTIONS** (should fix):

**NITPICKS** (optional):

**VERDICT**: REQUEST CHANGES / APPROVE WITH SUGGESTIONS / APPROVE
**CONFIDENCE**: HIGH / MEDIUM / LOW (LOW if similar files found don't seem relevant)
"""

    return prompt
