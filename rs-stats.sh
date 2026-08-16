#!/bin/bash
# Returns: CPU%|MEM%|DISK_GB_FREE|CTN_COUNT|UP_TIME
set -e
CPU=$(top -bn1 | awk '/^%?Cpu\(s\)/ {print 100-$8; exit}' | xargs printf "%.0f")
MEM=$(free | awk '/^Mem:/ {printf "%.0f", $3/$2*100}')
DISK=$(df -BG / 2>/dev/null | awk '$NF=="/" {gsub("G","",$4); print $4}' | head -1)
DISK=${DISK:-0}
CTN=$(docker ps -q 2>/dev/null | wc -l)
UP=$(uptime -p 2>/dev/null | sed 's/^up //')
echo "$CPU|$MEM|$DISK|$CTN|$UP"
