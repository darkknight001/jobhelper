---
name: job-fit-scoring
description: Judge how well a job posting fits a specific candidate and decide apply / review / skip. Use whenever the user shares a job link, JD or list of postings and asks "is this a fit", "should I apply", "score these jobs", "rank these roles", or when the JobHelper harness scores discovered jobs. Produces a calibrated 0-100 score with reasons, gaps and red flags.
---

# Job fit scoring

Score a posting the way a sharp technical recruiter who knows the candidate well would:
calibrated, specific, and honest. A high score means the candidate would be a top-decile
applicant *and* would plausibly want the job.

## 1. Get the candidate facts first

Never score from memory or assumptions. Load, in order:

1. In the JobHelper repo: `data/kb/*.md` (resume, claude.ai export, notes) and
   `data/profile.yaml` (location, preferences, notice period, compensation).
2. Otherwise: the resume or KB the user attached or pasted. If you have nothing, ask for it.

Pull out: years of experience, current title and level, primary language(s), core stack,
domains, notable scale, preferred roles, preferred locations and work modes, deal-breakers.

## 2. Read the posting for what actually matters

- **Role and level:** title, but also scope words (owns, leads, mentors, architecture,
  on-call, team size). Titles inflate and deflate across companies.
- **Must-have stack:** the language and frameworks the team actually works in day to day.
  "Python or Java" is not "Java".
- **Experience band:** the stated range, and whether the scope matches it.
- **Location / mode:** city, hybrid days, and for remote roles *which countries* may apply.
- **Company signal:** product company vs staffing agency vs vague consultancy; funding/size if stated.

## 3. Score with this rubric

| Score | Meaning |
|---|---|
| 90-100 | Role, seniority, core stack and location all fit. Clear top applicant. |
| 80-89 | Strong fit, only minor gaps (one secondary tool, adjacent domain). |
| 65-79 | Plausible but with a real gap: different primary language, seniority unclear or one level off, domain far from experience. |
| 40-64 | Weak fit: several gaps, or a role the candidate didn't ask for. |
| 0-39 | Wrong role. |

**Caps (apply the lowest that fits):**
- Primary language is Java / Go / Node / .NET and the candidate's language is absent → at most 70.
- Requires clearly more years than the candidate has plus about 3 → at most 65.
- Location outside the candidate's preferences and not remote-eligible for their country → at most 40, `location_ok = false`.
- The posting is mostly sales, pre-sales, solutions consulting, support or QA → at most 40.

**Red flags** (list them, and don't auto-apply when any are present): staffing agency with no
named client, "immediate joiner only" when the candidate has a long notice period,
fresher/junior signals, unpaid trial work, requests for payment, a mismatch between title
and responsibilities, a posting older than ~30 days.

Calibration checks before answering:
- Would you put money on this candidate clearing the recruiter screen? If not, it isn't 85+.
- Scores across a batch should spread out; ten jobs at 82 means you aren't discriminating.
- Every reason must point at a specific line in the posting and a specific fact about the candidate.

## 4. Decide

Default thresholds (JobHelper settings may override): **≥ 85 → apply automatically**,
**60-84 → human review**, **< 60 → skip**. Any red flag or `location_ok = false` sends
an otherwise-85+ job to review.

## 5. Output

**Harness / JSON:** one object per job:
`job_id, score, verdict (strong|good|stretch|weak|reject), summary (one phone-readable sentence),
reasons[≤4], gaps[≤4], red_flags[≤4], seniority_fit (under|fit|over|unclear), location_ok`,
plus `resume_variant` when several resumes exist (pick the one whose framing matches the role:
lead/architecture vs hands-on IC).

**Chat:** a short verdict line ("82 / review: strong Python platform fit, but Go-first team"),
then the reasons, gaps and red flags as bullets, then one line on how to position the
application if they go ahead.

Never invent facts about the candidate or the company. If the posting is too thin to judge,
say so and score conservatively.
