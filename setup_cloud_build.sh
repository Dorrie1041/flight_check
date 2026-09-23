#!/usr/bin/env bash
set -euo pipefail

: "${GCP_PROJECT:?Set GCP_PROJECT}"
: "${GCP_REGION:=us-west1}"
: "${STATE_BUCKET:=${GCP_PROJECT}-flight-check-state}"

BUILD_ACCOUNT_NAME="flight-check-builder"
BUILD_ACCOUNT="${BUILD_ACCOUNT_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"
RUNTIME_ACCOUNT="flight-check-runner@${GCP_PROJECT}.iam.gserviceaccount.com"

gcloud config set project "${GCP_PROJECT}"
gcloud services enable \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  run.googleapis.com \
  logging.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  storage.googleapis.com

gcloud iam service-accounts describe "${BUILD_ACCOUNT}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "${BUILD_ACCOUNT_NAME}" \
    --display-name "Flight check Cloud Build deployer"

gcloud iam service-accounts describe "${RUNTIME_ACCOUNT}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create flight-check-runner \
    --display-name "Flight check runner"

gcloud artifacts repositories describe flight-check --location "${GCP_REGION}" >/dev/null 2>&1 || \
  gcloud artifacts repositories create flight-check \
    --repository-format docker \
    --location "${GCP_REGION}"

gcloud storage buckets describe "gs://${STATE_BUCKET}" >/dev/null 2>&1 || \
  gcloud storage buckets create "gs://${STATE_BUCKET}" \
    --location "${GCP_REGION}" \
    --uniform-bucket-level-access

gcloud storage buckets add-iam-policy-binding "gs://${STATE_BUCKET}" \
  --member "serviceAccount:${RUNTIME_ACCOUNT}" \
  --role roles/storage.objectUser

for role in \
  roles/artifactregistry.writer \
  roles/run.admin \
  roles/logging.logWriter \
  roles/cloudscheduler.admin; do
  gcloud projects add-iam-policy-binding "${GCP_PROJECT}" \
    --member "serviceAccount:${BUILD_ACCOUNT}" \
    --role "${role}" \
    --condition=None
done

gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_ACCOUNT}" \
  --member "serviceAccount:${BUILD_ACCOUNT}" \
  --role roles/iam.serviceAccountUser

for secret in openai-api-key telegram-bot-token telegram-chat-id; do
  if ! gcloud secrets describe "${secret}" >/dev/null 2>&1; then
    echo "Missing Secret Manager secret: ${secret}" >&2
    echo "Create it, then rerun this script." >&2
    exit 1
  fi
  gcloud secrets add-iam-policy-binding "${secret}" \
    --member "serviceAccount:${RUNTIME_ACCOUNT}" \
    --role roles/secretmanager.secretAccessor
done

echo
echo "Cloud Build service account is ready:"
echo "projects/${GCP_PROJECT}/serviceAccounts/${BUILD_ACCOUNT}"
echo
echo "Use this account when creating the flight-check-main Cloud Build trigger."
echo "Repository: Dorrie1041/flight_check"
echo "Branch pattern: ^main$"
echo "Build configuration: cloudbuild.yaml"
echo "Region: ${GCP_REGION}"
echo "State bucket: ${STATE_BUCKET}"
