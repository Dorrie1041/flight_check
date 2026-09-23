#!/usr/bin/env bash
set -euo pipefail

# Required before running:
#   export GCP_PROJECT="your-project-id"
#   export GCP_REGION="us-west1"
#   export STATE_BUCKET="your-unique-bucket-name"
#   export FLIGHT_ORIGIN="SFO"
#   export FLIGHT_DESTINATION="WUH"
#   export FLIGHT_DEPARTURE="2027-01-04"
#   export FLIGHT_RETURN="2027-02-08"
# Create Secret Manager secrets named openai-api-key, telegram-bot-token,
# and telegram-chat-id before running this script.

: "${GCP_PROJECT:?Set GCP_PROJECT}"
: "${GCP_REGION:=us-west1}"
: "${STATE_BUCKET:?Set STATE_BUCKET}"
: "${FLIGHT_ORIGIN:?Set FLIGHT_ORIGIN}"
: "${FLIGHT_DESTINATION:?Set FLIGHT_DESTINATION}"
: "${FLIGHT_DEPARTURE:?Set FLIGHT_DEPARTURE}"
: "${FLIGHT_RETURN:?Set FLIGHT_RETURN}"

JOB_NAME="flight-check"
REPOSITORY="flight-check"
SCHEDULER_JOB_NAME="${SCHEDULER_JOB_NAME:-flight-check-tuesday-9am}"
SCHEDULER_SCHEDULE="${SCHEDULER_SCHEDULE:-0 9 * * 2}"
SCHEDULER_TIME_ZONE="${SCHEDULER_TIME_ZONE:-America/Los_Angeles}"
IMAGE="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/${REPOSITORY}/flight-check:latest"
SERVICE_ACCOUNT="flight-check-runner@${GCP_PROJECT}.iam.gserviceaccount.com"
RUN_JOB_URI="https://run.googleapis.com/v2/projects/${GCP_PROJECT}/locations/${GCP_REGION}/jobs/${JOB_NAME}:run"

gcloud config set project "${GCP_PROJECT}"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com storage.googleapis.com

gcloud artifacts repositories describe "${REPOSITORY}" --location "${GCP_REGION}" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "${REPOSITORY}" --repository-format docker --location "${GCP_REGION}"

gcloud iam service-accounts describe "${SERVICE_ACCOUNT}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create flight-check-runner --display-name "Flight check runner"

gcloud storage buckets describe "gs://${STATE_BUCKET}" >/dev/null 2>&1 || \
  gcloud storage buckets create "gs://${STATE_BUCKET}" --location "${GCP_REGION}" --uniform-bucket-level-access

gcloud storage buckets add-iam-policy-binding "gs://${STATE_BUCKET}" \
  --member "serviceAccount:${SERVICE_ACCOUNT}" --role roles/storage.objectUser

for secret in openai-api-key telegram-bot-token telegram-chat-id; do
  gcloud secrets add-iam-policy-binding "${secret}" \
    --member "serviceAccount:${SERVICE_ACCOUNT}" --role roles/secretmanager.secretAccessor
done

gcloud builds submit --tag "${IMAGE}" .

gcloud run jobs deploy "${JOB_NAME}" \
  --image "${IMAGE}" \
  --region "${GCP_REGION}" \
  --service-account "${SERVICE_ACCOUNT}" \
  --task-timeout 10m \
  --memory 2Gi \
  --cpu 1 \
  --max-retries 1 \
  --set-env-vars "FLIGHT_ORIGIN=${FLIGHT_ORIGIN},FLIGHT_DESTINATION=${FLIGHT_DESTINATION},FLIGHT_DEPARTURE=${FLIGHT_DEPARTURE},FLIGHT_RETURN=${FLIGHT_RETURN},FLIGHT_ADULTS=1,FLIGHT_FLEXIBLE=true,USE_LLM=true,TELEGRAM_NOTIFY=true,NOTIFY_MODE=change,STATE_BUCKET=${STATE_BUCKET},OPENAI_MODEL=gpt-4o-mini" \
  --set-secrets "OPENAI_API_KEY=openai-api-key:latest,TELEGRAM_BOT_TOKEN=telegram-bot-token:latest,TELEGRAM_CHAT_ID=telegram-chat-id:latest"

gcloud run jobs add-iam-policy-binding "${JOB_NAME}" \
  --region "${GCP_REGION}" \
  --member "serviceAccount:${SERVICE_ACCOUNT}" \
  --role roles/run.invoker

if gcloud scheduler jobs describe "${SCHEDULER_JOB_NAME}" --location "${GCP_REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${SCHEDULER_JOB_NAME}" \
    --location "${GCP_REGION}" \
    --schedule "${SCHEDULER_SCHEDULE}" \
    --time-zone "${SCHEDULER_TIME_ZONE}" \
    --uri "${RUN_JOB_URI}" \
    --http-method POST \
    --oauth-service-account-email "${SERVICE_ACCOUNT}"
else
  gcloud scheduler jobs create http "${SCHEDULER_JOB_NAME}" \
    --location "${GCP_REGION}" \
    --schedule "${SCHEDULER_SCHEDULE}" \
    --time-zone "${SCHEDULER_TIME_ZONE}" \
    --uri "${RUN_JOB_URI}" \
    --http-method POST \
    --oauth-service-account-email "${SERVICE_ACCOUNT}"
fi

echo "Deployed ${JOB_NAME}. Test it with:"
echo "gcloud run jobs execute ${JOB_NAME} --region ${GCP_REGION} --wait"
echo
echo "Scheduled ${SCHEDULER_JOB_NAME} for every Tuesday at 9:00 AM (${SCHEDULER_TIME_ZONE})."
