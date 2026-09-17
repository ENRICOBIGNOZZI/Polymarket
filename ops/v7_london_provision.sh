#!/usr/bin/env bash
set -euo pipefail

EXPECTED_SHA="${POLYMARKET_EXPECTED_SHA:?POLYMARKET_EXPECTED_SHA is required}"
REGION="${AWS_REGION:-eu-west-2}"
STACK="${POLYMARKET_LONDON_STACK:-polymarket-v7-london-shootout}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POLICY="$ROOT/config/v7_london_az_shootout.json"
TEMPLATE="$ROOT/infra/aws/v7_london_shootout.json"
OUTPUT_DIR="${POLYMARKET_LONDON_OUTPUT_DIR:-$HOME/polymarket-london}"
SERVICE_USER="${POLYMARKET_LONDON_SERVICE_USER:-ubuntu}"

fail(){ echo "v7_london_provision: $*" >&2; exit 2; }
[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "exact lowercase 40-char SHA required"
[[ "$REGION" == eu-west-2 ]] || fail "London shootout requires eu-west-2"
command -v aws >/dev/null 2>&1 || fail "AWS CLI required"
[[ -f "$POLICY" && -f "$TEMPLATE" ]] || fail "London policy/template missing"

identity="$(aws sts get-caller-identity --region "$REGION" --output json)" || fail "AWS identity unavailable"
account="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Account"])' <<<"$identity")"
arn="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Arn"])' <<<"$identity")"
[[ -n "$account" && -n "$arn" ]] || fail "AWS caller identity incomplete"

zones_json="$(aws ec2 describe-availability-zones --region "$REGION" --all-availability-zones --output json)"
zone_rows=()
while IFS= read -r row; do zone_rows+=("$row"); done < <(python3 -c 'import json,sys
v=json.load(sys.stdin); wanted={"euw2-az1","euw2-az2","euw2-az3"}; rows=[]
for z in v.get("AvailabilityZones",[]):
    zid=z.get("ZoneId"); name=z.get("ZoneName"); state=z.get("State")
    if zid in wanted and state=="available" and isinstance(name,str): rows.append((zid,name))
for zid,name in sorted(rows): print(zid+"\t"+name)' <<<"$zones_json")
[[ "${#zone_rows[@]}" == 3 ]] || fail "all physical AZ IDs euw2-az1/2/3 must be available"
zone_names=()
for row in "${zone_rows[@]}"; do zone_names+=("${row#*$'\t'}"); done

preferences=()
while IFS= read -r candidate; do preferences+=("$candidate"); done < <(python3 - "$POLICY" <<'PY'
import json,sys
v=json.load(open(sys.argv[1],encoding='utf-8'))
for x in v.get('instance_type_preferences',[]):
    if isinstance(x,str) and x: print(x)
PY
)
[[ "${#preferences[@]}" -gt 0 ]] || fail "instance type preference list empty"
instance_type=""
for candidate in "${preferences[@]}"; do
  offerings="$(aws ec2 describe-instance-type-offerings --region "$REGION" --location-type availability-zone \
    --filters "Name=instance-type,Values=$candidate" --output json)"
  if OFFERINGS_JSON="$offerings" python3 - "${zone_names[@]}" <<'PY'
import json,os,sys
wanted=set(sys.argv[1:]); v=json.loads(os.environ['OFFERINGS_JSON'])
have={x.get('Location') for x in v.get('InstanceTypeOfferings',[]) if x.get('InstanceType')}
raise SystemExit(0 if wanted <= have else 1)
PY
  then instance_type="$candidate"; break; fi
done
[[ -n "$instance_type" ]] || fail "no preferred instance type is offered in all three physical AZs"

images="$(aws ec2 describe-images --region "$REGION" --owners 099720109477 \
  --filters 'Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*' \
            'Name=architecture,Values=x86_64' 'Name=state,Values=available' \
            'Name=root-device-type,Values=ebs' --output json)"
image_id="$(python3 -c 'import json,sys
v=json.load(sys.stdin); rows=[x for x in v.get("Images",[]) if x.get("ImageId") and x.get("CreationDate")]
print(max(rows,key=lambda x:x["CreationDate"])["ImageId"] if rows else "")' <<<"$images")"
[[ "$image_id" =~ ^ami-[0-9a-f]+$ ]] || fail "could not resolve Canonical Ubuntu 24.04 gp3 amd64 AMI"

aws cloudformation deploy --region "$REGION" --stack-name "$STACK" --template-file "$TEMPLATE" \
  --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset \
  --parameter-overrides "ExpectedSha=$EXPECTED_SHA" "ImageId=$image_id" \
                        "InstanceType=$instance_type" "ServiceUser=$SERVICE_USER"

stack_json="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" --output json)"
mkdir -p "$OUTPUT_DIR"
receipt="$OUTPUT_DIR/provision.$EXPECTED_SHA.json"
STACK_JSON="$stack_json" python3 - "$receipt" "$EXPECTED_SHA" "$REGION" "$STACK" \
  "$account" "$arn" "$instance_type" "$image_id" "${zone_rows[@]}" <<'PY'
import json,os,sys,time
from pathlib import Path
path,sha,region,stack,account,arn,itype,image,*zones=sys.argv[1:]
v=json.loads(os.environ['STACK_JSON']); s=v['Stacks'][0]
out={x['OutputKey']:x['OutputValue'] for x in s.get('Outputs',[])}
value={'schema':'polymarket_v7_london_provision_receipt_v1','timestamp':int(time.time()),
       'expected_sha':sha,'region':region,'stack':stack,'aws_account':account,'caller_arn':arn,
       'instance_type':itype,'image_id':image,
       'physical_zone_mapping':[dict(zip(('zone_id','account_zone_name'),z.split('\t',1))) for z in zones],
       'stack_outputs':out,'paper_only':True,'authenticated_execution':False,
       'real_order_submission':False,'automatic_cutover':False,
       'inbound_security_group_rules':0,
       'admin_transport':'AWS_SSM_OR_SEPARATELY_AUTHENTICATED_TAILSCALE'}
Path(path).write_text(json.dumps(value,sort_keys=True,indent=2)+'\n',encoding='utf-8')
print(json.dumps(value,sort_keys=True,indent=2))
PY

echo "receipt=$receipt"
echo "Three London benchmark hosts are provisioned with runtime services disabled."
echo "Use SSM or separately authenticated Tailscale for administration; do not enable real-order authority."
