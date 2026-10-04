#!/bin/bash
# slot-hook.sh: the Mac's job slots, shared by the native macOS runner and the Colima VM's runner. Each runner
# points ACTIONS_RUNNER_HOOK_JOB_STARTED at a link to this file named job-started.sh and
# ACTIONS_RUNNER_HOOK_JOB_COMPLETED at one named job-completed.sh (the runner gives hooks no arguments, so the
# name says which). The Mac allows N jobs at once (`runner slots air-1 N`, default 1): the slots are the
# directories $GITRUNNER_SLOT_DIR/slot-1 .. slot-N, and `mkdir` is atomic, on the Mac's disk and through the VM's
# virtiofs mount alike, so no two jobs ever share a slot. A job takes a slot only while the number of held
# slot-* folders (all of them, whatever their number, so lowering N never evicts a running job) is below N;
# otherwise it WAITS here (on GitHub it shows as running meanwhile) until one is free: the extra job is delayed,
# never concurrent. N is read from $GITRUNNER_SLOT_DIR/slots.conf (`slots=N` or `slots=off`; missing = 1) on
# every poll, so a change takes effect at once. Inside a slot the file `owner` says who holds it. A slot whose
# holder's job is gone (cancelled, runner crashed) is cleared by the Mac's watcher loop (gitrunner linux-vm), and a
# runner that finds a slot with its own name knows it is left over from its own earlier job.
# A lock folder from before slots (`lock`) is renamed to slot-1.
#   env: SLOT_OWNER (the runner's name), GITRUNNER_SLOT_DIR, SLOT_POLL (s, default 3), SLOT_MAX_WAIT (s, default 7200)
set -eu
SLOT=${GITRUNNER_SLOT_DIR:-/private/var/gitrunner-slot}
OWNER=${SLOT_OWNER:-${RUNNER_NAME:-$(hostname)}}
POLL=${SLOT_POLL:-3}
MAX=${SLOT_MAX_WAIT:-7200}
SIDE=mac; [ "$(uname)" = Darwin ] || SIDE=linux

say() { printf '%s slot-hook: %s\n' "$(date '+%F %T')" "$*"; }
slots_n() {  # N, or off; a missing or odd file means the safe default of 1
  local v
  v=$(sed -n 's/^slots=\([0-9][0-9]*\|off\)$/\1/p' "$SLOT/slots.conf" 2>/dev/null | head -1 || true)
  echo "${v:-1}"
}
held() { local d; for d in "$SLOT"/slot-*; do [ -d "$d" ] && echo "$d"; done; return 0; }
holder() { sed -n 's/^runner=\([^ ]*\).*/\1/p' "$1/owner" 2>/dev/null | head -1 || true; }
migrate() { [ ! -d "$SLOT/lock" ] || [ -e "$SLOT/slot-1" ] || mv "$SLOT/lock" "$SLOT/slot-1" 2>/dev/null || true; }
drop_mine() { local d; for d in $(held); do [ "$(holder "$d")" = "$OWNER" ] && rm -rf "$d"; done; return 0; }

acquire() {
  local start=$SECONDS noted=0 n i d cnt
  [ -d "$SLOT" ] || { say "no slot folder $SLOT: not gating this job"; return 0; }
  migrate
  n=$(slots_n); [ "$n" != off ] || { say "job slots are off: not gating this job"; return 0; }
  drop_mine   # a runner runs one job at a time: a slot still in its name is left by a job whose completed hook never ran
  while :; do
    n=$(slots_n); [ "$n" != off ] || return 0
    cnt=$(held | wc -l | tr -d ' ')
    if [ "$cnt" -lt "$n" ]; then
      for i in $(seq 1 "$n"); do
        d=$SLOT/slot-$i
        mkdir "$d" 2>/dev/null || continue
        printf 'runner=%s\nside=%s\njob=%s/%s\npid=%s\ntime=%s\nblock=%s\n' "$OWNER" "$SIDE" "${GITHUB_RUN_ID:-?}" "${GITHUB_JOB:-?}" "$$" "$(date +%s)" "$((i - 1))" > "$d/owner"
        if [ "$(held | wc -l | tr -d ' ')" -gt "$n" ]; then   # another job (or a lowered N) won the same instant: back out
          rm -rf "$d"; sleep "0.$((RANDOM % 5 + 1))"; continue 2
        fi
        say "got the Mac's job slot $i of $n ($OWNER)"
        return 0
      done
    fi
    [ "$noted" = 1 ] || { say "waiting for the Mac's job slot ($cnt of $n in use)"; noted=1; }
    if [ $((SECONDS - start)) -ge "$MAX" ]; then say "still no slot after ${MAX}s: giving up"; return 1; fi
    sleep "$POLL"
  done
}

release() {
  local d
  [ -d "$SLOT" ] || return 0
  migrate
  for d in $(held); do
    if [ "$(holder "$d")" = "$OWNER" ]; then rm -rf "$d"; say "released the Mac's job slot ${d##*-} ($OWNER)"; fi
  done
}

case ${0##*/} in
  *started*) acquire ;;
  *completed*) release ;;
  *) case ${1:-} in acquire) acquire ;; release) release ;; *) say "usage: job-started.sh | job-completed.sh | slot-hook.sh acquire|release"; exit 2 ;; esac ;;
esac
