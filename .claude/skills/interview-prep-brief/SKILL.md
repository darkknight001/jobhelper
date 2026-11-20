---
name: interview-prep-brief
description: Write a focused preparation brief for a company and role the candidate has been shortlisted, invited to interview, or sent an assessment for. Covers the company, what the role really needs, likely rounds, which of the candidate's stories to use, technical topics to revise, gaps to prepare for, and questions to ask. Use when the user says "prep me for X", "I got shortlisted at X", "interview at X next week", or when JobHelper detects a shortlist/interview/assessment email.
---

# Interview prep brief

Goal: in 10 minutes of reading on a phone, the candidate knows what this company will probe
and exactly which of their own experiences answer it.

## Inputs

1. The job posting (title, description, requirements).
2. The trigger email: round type, date, format, interviewer names.
3. The candidate: `data/kb/*.md` and `data/profile.yaml` in the JobHelper repo, or the resume/KB
   the user provides.
4. Light research when web tools are available: the company's product, business model, recent
   news (last 12 months), engineering blog, tech stack signals, public interview-process notes.
   Prefer primary sources (company site, eng blog, official announcements). Mark anything from
   forums such as Glassdoor or Reddit as anecdotal. Never present guesses as facts.

## Brief structure (Markdown, headings in this order)

1. **At a glance**: round, date/time, format, who; a three-line summary of the company and role.
2. **The company in 5 bullets**: what they sell, to whom, scale, recent news, engineering
   culture signals. Each with a source link when researched.
3. **What this role actually needs**: the 4-6 real requirements read from the JD, ranked.
4. **Your evidence map**: a table of *requirement → the candidate's best story or project → the
   metric to quote*. Use only KB facts.
5. **Likely rounds and what each tests**: recruiter screen, coding/DSA level, system design,
   low-level design, behavioural/leadership, hiring manager. Adapt to company size and level.
6. **Technical revision list**: 6-10 concrete topics tied to the JD and the company's domain,
   such as "design a rate-limited webhook delivery system" for a payments company. Include
   1-2 system-design prompts they are likely to get, with the angles to cover.
7. **Gaps and tough questions**: where the profile is thin for this role, and a truthful
   framing for each. Also "why leave now", "why this company", notice period and CTC
   expectations, using profile values only.
8. **Questions to ask them**: 5 sharp questions specific to this team and company.
9. **Logistics checklist**: links, time zone, tools to install, documents, and what to send
   the recruiter (the candidate sends it, not you).

## Rules

- Never invent the candidate's experience, numbers, or the company's facts.
- Keep it tight: about 600-900 words, scannable bullets and tables, no filler.
- No contacting anyone and no replying to emails; this brief is for the candidate only.
- If the role or company can't be identified confidently, say so at the top.
