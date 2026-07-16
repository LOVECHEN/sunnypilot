#!/usr/bin/env bash

# 本地 athena/api stub —— 零外网。由 system.athena.local_stub 提供服务,
# athenad 能真正握手成功,因此不会陷入重连循环、不会刷日志。
export ATHENA_HOST='ws://127.0.0.1:8899'
export API_HOST='http://127.0.0.1:8899'

# 免除开机繁琐确认
echo -n "2" > /data/params/d/HasAcceptedTerms
echo -n "1.0" > /data/params/d/HasAcceptedTermsSP
echo -n "0.2.0" > /data/params/d/CompletedTrainingVersion
echo -n "1.0" > /data/params/d/CompletedSunnylinkConsentVersion
echo -n "1" > /data/params/d/IsMetric

exec ./launch_chffrplus.sh
