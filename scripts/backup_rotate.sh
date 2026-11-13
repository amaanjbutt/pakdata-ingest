#!/bin/sh
# Rotate the nightly dumps in a backup directory (borg/restic-style "keep" rules):
#
#   keep the newest dump of each of the last BACKUP_KEEP_DAILY   days    (default 7)
#   keep the newest dump of each of the last BACKUP_KEEP_WEEKLY  ISO weeks (default 4)
#   keep the newest dump of each of the last BACKUP_KEEP_MONTHLY months  (default 3)
#
# Buckets are counted over the days/weeks/months that HAVE a dump, newest first, and overlap (the
# newest dump is daily, weekly and monthly at once), so 7/4/3 keeps ~12 files reaching ~3 months back
# for the disk of 12 dailies. Only files named pakdata_YYYYMMDD_HHMMSS.dump are considered; anything
# else in the directory is left alone. The newest dump is always kept.
#
#   backup_rotate.sh [--dry-run] [DIR]        (DIR defaults to /backups)
#
# Called by backup.sh after a dump passed its restore test. POSIX sh + GNU date (postgres:16 image).
set -eu

DRY=0
if [ "${1:-}" = "--dry-run" ]; then DRY=1; shift; fi
DIR=${1:-/backups}
D=${BACKUP_KEEP_DAILY:-7}
W=${BACKUP_KEEP_WEEKLY:-4}
M=${BACKUP_KEEP_MONTHLY:-3}
[ "$D" -ge 1 ] || D=1

files=$(ls -1 "$DIR" 2>/dev/null | grep -E '^pakdata_[0-9]{8}_[0-9]{6}\.dump$' | sort -r || true)
[ -n "$files" ] || exit 0

# "<file> <day> <iso-week> <month>", newest first -> the files to keep
keep=$(for f in $files; do
         d=${f#pakdata_}; d=${d%%_*}
         printf '%s %s %s %s\n' "$f" "$d" "$(date -u -d "$d" +%G-W%V)" "${d%??}"
       done | awk -v D="$D" -v W="$W" -v M="$M" '
         !($2 in day) && nd < D { day[$2] = 1; nd++; keep[$1] = 1 }
         !($3 in wk)  && nw < W { wk[$3] = 1;  nw++; keep[$1] = 1 }
         !($4 in mo)  && nm < M { mo[$4] = 1;  nm++; keep[$1] = 1 }
         # a later dump of an already-counted day/week/month must still mark the bucket as seen
         { day[$2] = 1; wk[$3] = 1; mo[$4] = 1; if ($1 in keep) print $1 }')

for f in $files; do
  case " $(echo $keep) " in
    *" $f "*) ;;
    *) if [ "$DRY" = 1 ]; then echo "would rotate out $DIR/$f"
       else echo "rotating out $DIR/$f"; rm -f "$DIR/$f"; fi ;;
  esac
done
