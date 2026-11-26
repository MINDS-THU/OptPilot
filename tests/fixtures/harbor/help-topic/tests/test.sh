#!/bin/sh
mkdir -p /logs/verifier
if [ -f /app/topic.txt ] && [ "$(cat /app/topic.txt)" = 'work card' ]; then
    echo 1 > /logs/verifier/reward.txt
else
    echo 0 > /logs/verifier/reward.txt
fi
