# Exporting your career knowledge from claude.ai

JobHelper can't read your claude.ai chats or memory, so ask claude.ai to write it out
once and paste the result in.

## Steps

1. Open **claude.ai** in a new chat. If you keep job-search chats in a Project, open the
   chat inside that Project so it can see the Project knowledge too.
2. Make sure memory / "search past chats" is on (Settings → Capabilities), so it can pull
   from earlier conversations.
3. Paste the prompt below and send it. If the answer gets cut off, reply `continue`.
4. Copy the whole answer and either:
   - paste it into the dashboard: **Knowledge → Import** (it's saved as
     `data/kb/claude-export.md`), or
   - paste it back into the Claude Code session that's building JobHelper.

Run it again whenever claude.ai has learned something new; a re-import replaces the old export.

## The prompt

```text
I'm building a personal job-application assistant that fills in job applications and
answers recruiter screening questions on my behalf. Write out everything you know about
me that is relevant to my career, drawing on your memory of me and our past
conversations (and this Project's knowledge, if any).

Rules:
- Only include facts you actually have from me. Do not invent, round up, or generalise.
- If something is uncertain or may be outdated, append "(unverified)" or "(as of <month year>)".
- Where you know the context behind a fact (why I left a job, what I want next), include it.
- Use exactly the Markdown headings below. Leave a section out entirely if you know nothing for it.
- Be detailed: specific numbers, tools, team sizes, outcomes, dates.

# Identity & logistics
Current location, willingness to relocate (and where), notice period, current CTC,
expected CTC, earliest joining date, work-mode preference (remote/hybrid/onsite),
work authorisation, languages.

# Career goals
Target roles and seniority, the kind of companies/domains I want and don't want,
deal-breakers, what I'm optimising for (money, learning, title, WLB, etc.).

# Work history
For each role: company, title, dates, team size, reporting line, what I owned,
key projects with measurable outcomes, technologies, and why I left or want to leave.

# Projects (work and personal)
Name, problem, my role, architecture/tech, scale, outcome, links if any.
Include side projects (hardware, homelab, AI agents, etc.).

# Skills
Group by: languages, back-end frameworks, data stores, cloud/infra, messaging/streaming,
observability, AI/LLM, tools. Mark my strongest areas and anything I'm currently learning.

# Achievements & recognition
Awards, promotions, performance ratings, notable wins, mentoring.

# Education & certifications
Degrees, institutes, years, grades if known; certifications and courses.

# Interview prep & preferences
Stories I've prepared (STAR format if we discussed them), system-design topics I'm
strong or weak in, DSA practice status, salary negotiation notes.

# Standard screening answers
Short, ready-to-use answers for questions like: "Why are you looking for a change?",
"Tell me about yourself", "Why this role?", "Biggest achievement", "Preferred location",
"Are you open to relocation?", "Notice period / can you join early?".

# Anything else
Other facts about me that a recruiter or an application form might ask about.
```
