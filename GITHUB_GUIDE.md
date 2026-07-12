# GitHub Guide — versioning this project, drop by drop

Written for this repo specifically. Commands assume Ubuntu; run them in the
repo root (`~/fpga-tick`). The repo you unzipped already has git history
started (one commit, tagged `v1.0`) — you only need to connect it to GitHub
and learn the per-drop rhythm.

## 0. The mental model (five words each)

| Concept | What it is | In this project |
|---|---|---|
| repository | the project + its entire history | the `fpga-tick` folder (history lives in the hidden `.git/`) |
| commit | one immutable snapshot with a message | one "drop" |
| tag | a permanent name on one commit | `v1.0`, `v1.1-notifications`, ... |
| branch | a movable line of commits | `main` for finished drops; feature branches for experiments |
| remote | a copy elsewhere (GitHub) | your backup + portfolio |
| push / pull | send / fetch commits to / from the remote | end-of-session sync |

A commit is a snapshot of the WHOLE tree, not a diff — checking out an old
tag gives you the complete project exactly as it was, which is precisely
your "different Rev" request. Git stores deltas internally; you never think
about that.

## 1. One-time machine setup

```bash
sudo apt install git
git config --global user.name  "Ming Lin"
git config --global user.email "you@example.com"   # use your GitHub email
git config --global init.defaultBranch main
```

Authentication — SSH keys, one-time:
```bash
ssh-keygen -t ed25519 -C "you@example.com"    # accept defaults, passphrase optional
cat ~/.ssh/id_ed25519.pub                     # copy the whole line
```
GitHub → your avatar → **Settings → SSH and GPG keys → New SSH key** →
paste → save. Test: `ssh -T git@github.com` should greet you by username.

## 2. Create the GitHub repo and connect this one

On github.com: **New repository** → name it (suggestion:
`fpga-tick-engine`) → **do NOT initialize** with a README/.gitignore (this
repo already has both — an initialized remote just creates a merge
headache). Public vs private: public makes it a portfolio piece for the
FPGA-role search — the README's test census and the CODE_WALKTHROUGH are
exactly what a hiring engineer wants to see. The .gitignore already keeps
keys, audit logs, and kill markers out; double-check nothing sensitive
slipped in with `git log --stat` before pushing.

Then, from the repo root:
```bash
git remote add origin git@github.com:YOURUSER/fpga-tick-engine.git
git push -u origin main       # uploads the v1.0 commit
git push --tags               # uploads the v1.0 tag
```
Refresh the GitHub page — README renders on the front page; the Tags tab
shows v1.0.

## 3. The per-drop rhythm (this is 95% of daily git)

Say the next drop is ntfy.sh notifications. Work normally, then:

```bash
git status                          # what changed? (untracked = new files)
git diff                            # review every modified line — the
                                    #   pre-commit self-review habit is the
                                    #   single highest-value git discipline
git add host/notify.py host/test_notify.py host/order_manager.py README.md
git commit -m "Drop 10: ntfy.sh push notifications on fills and kills

- notify.py: one HTTP POST per event, stdlib only
- wired into OrderManager fill/halt paths
- 12 new checks; all regressions pass (3002 total)"
git tag -a v1.1 -m "notifications"
git push && git push --tags
```

Commit-message convention that pays off later: one summary line (what),
blank line, bullet details (why / test impact). Your future self reading
`git log --oneline` six months from now is the audience.

Add a row to CHANGELOG.md in the same commit — the changelog explains
drops to humans; tags let git jump to them.

## 4. Using the history (the payoff)

```bash
git log --oneline --graph           # the whole story, one line per drop
git show v1.0 -- rtl/top_arty.sv    # a file AS IT WAS at v1.0
git diff v1.0 v1.1                  # everything a drop changed
git checkout v1.0                   # time-travel the working tree (read-only
                                    #   "detached HEAD" — look around, then:)
git checkout main                   # ...come back to the present
git blame rtl/ema_engine.sv         # which commit last touched each line
```

`git checkout <tag>` is the answer to "give me Rev N": the entire tree —
RTL, testbenches, host code — exactly as tagged. Rebuild that bitstream,
rerun those tests.

## 5. Branches — when a drop is risky

For an experiment that might not pan out (say, a CRC-8 wire-format change
that touches parser + testbenches + host codec):

```bash
git checkout -b crc8-experiment     # fork off; main stays safe
# ...hack, commit as often as you like, even broken states...
git checkout main                   # main is untouched the whole time
git merge crc8-experiment           # experiment worked: fold it in
git branch -d crc8-experiment       # done with the branch
```
If it didn't work out: just delete the branch — main never knew.

## 6. VS Code integration

You already have the right editor: the Source Control icon (Ctrl+Shift+G)
shows changed files with inline diffs; stage with `+`, type the message,
Ctrl+Enter to commit, "Sync Changes" to push. The gutter marks
added/changed lines as you edit. Learn the CLI first (it's what every
guide, error message, and interview assumes), then let VS Code speed up
the daily loop.

## 7. Recovering the original drop snapshots (optional)

If you still have the per-drop zips downloaded from our sessions
(fpga-tick-integration.zip, fpga-tick-tx-layer.zip, ...), you can
reconstruct the true historical revisions BEFORE pushing:

```bash
git checkout --orphan history       # empty parallel timeline
git rm -rf . && rm -rf *            # clear the working tree
unzip -o ~/Downloads/fpga-tick-integration.zip -d /tmp/d1
cp -r /tmp/d1/fpga-tick/* . && git add -A
git commit -m "Drop 1: RX layer" && git tag v0.1
# ...repeat: unzip drop N over the tree, add -A, commit, tag v0.N...
```
Each zip was a complete snapshot, so this recreates buildable history.
Entirely optional — the CHANGELOG already records the story, and v1.0
onward is where the living history grows.

## 8. Rules of the road

- Commit at every green-test checkpoint, not once a week. Small commits
  are cheap; untangling a giant one is not.
- Never commit secrets. The .gitignore guards .env; keep ALPACA keys in
  environment variables or an ignored file, never in code. If a key ever
  lands in a commit, treat it as leaked: revoke it at Alpaca and issue a
  new one — deleting the file in a later commit does NOT remove it from
  history.
- Never commit derived junk: Vivado project directories, .vcd waves,
  sim.out — all regenerable, all already ignored. The repo holds sources
  and constraints; anyone (including future-you) recreates the project
  from rtl/ + constraints/ in five minutes.
- `git push` at the end of every session. The remote is your backup; a
  dead SSD should cost you nothing but hardware.
