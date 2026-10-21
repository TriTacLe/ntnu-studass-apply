# ntnu-studass-apply

Registers læringsassistent applications in the IE faculty studass portal
(https://ie.ntnu.no/studass/) from the shell, one application per course, in a priority
order you control.

## The portal only answers from inside NTNU

Measured 21.10.2026 from a connection in Cape Town: `www.ntnu.no` and `innsida.ntnu.no`
both return 200, `129.241.148.101` (ie.ntnu.no) replies to ping at 182 ms, but TCP to
its port 443 and 80 never completes. The host filters off-campus addresses. Bring up the
NTNU VPN before anything else:

    openconnect --protocol=anyconnect vpn.ntnu.no

`studass.py preflight` checks this and every other command refuses to run without it.

## Setup

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/playwright install chromium

## Use

    ./studass.py preflight                      # VPN and host reachability
    ./studass.py login                          # headed, you do Feide once
    ./studass.py dump                           # raw HTML of the three pages into out/raw/
    ./studass.py list                           # round, deadline, every course on offer
    ./studass.py apply --all                    # dry run, prints what it would submit
    ./studass.py apply --all --submit           # registers one application per course
    ./studass.py apply --all --exclude IDATT2105 --submit
    ./studass.py apply --only IDATT2106,TDT4114 --submit
    ./studass.py status                         # Aktive søknader
    ./studass.py profile --laos ~/laos.pdf      # uploads the LAOS kursbevis

`login` opens a real window, you finish Feide yourself, and the Chromium profile under
`~/.local/share/studass/profile` keeps the session for later runs. The script never reads,
types or stores a password.

`apply` is a dry run unless `--submit` is given. Submission order carries the priority, and
the portal lets you reprioritise until the deadline. Courses already in Aktive søknader are
skipped, so a rerun is safe.

## Two things it deliberately will not do

- It never types a personnummer. `profile --laos` only uploads the certificate; if the
  portal refuses to save because the personnummer field is empty, it stops and tells you.
- `dump` strips eleven-digit numbers and the personnummer input value before writing the
  profile page to disk.

## Files

- `comment.txt` the text pasted into every application
- `priority.txt` course codes, strongest first; anything the portal offers that is not
  listed here goes after them
- `out/` dumps, course JSON and screenshots, all gitignored

## Exit codes

| Code | Meaning |
|---|---|
| 0 | fine |
| 2 | no session, run `login` |
| 3 | the spring round is not open |
| 4 | a submit could not be confirmed |
| 5 | portal unreachable, check the VPN |
