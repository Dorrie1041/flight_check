#!/usr/bin/env bash
set -euo pipefail

: "${GCP_PROJECT:?Set GCP_PROJECT}"
: "${GCP_REGION:=us-west1}"

BUILD_ACCOUNT_NAME="flight-check-builder"
BUILD_ACCOUNT="${BUILD_ACCOUNT_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"
RUNTIME_ACCOUNT="flight-check-runner@${GCP_PROJECT}.iam.gserviceaccount.com"

gcloud config set project "${GCP_PROJECT}"
gcloud services enable \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  run.googleapis.com \
  logging.googleapis.com

gcloud iam service-accounts describe "${BUILD_ACCOUNT}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "${BUILD_ACCOUNT_NAME}" \
    --display-name "Flight check Cloud Build deployer"

gcloud iam service-accounts describe "${RUNTIME_ACCOUNT}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create flight-check-runner \
    --display-name "Flight check runner"

for role in roles/artifactregistry.writer roles/run.admin roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "${GCP_PROJECT}" \
    --member "serviceAccount:${BUILD_ACCOUNT}" \
    --role "${role}" \
    --condition=None
done

gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_ACCOUNT}" \
  --member "serviceAccount:${BUILD_ACCOUNT}" \
  --role roles/iam.serviceAccountUser

echo
echo "Cloud Build service account is ready:"
echo "projects/${GCP_PROJECT}/serviceAccounts/${BUILD_ACCOUNT}"
echo
echo "Use this account when creating the flight-check-main Cloud Build trigger."
echo "Repository: Dorrie1041/flight_check"
echo "Branch pattern: ^main$"
echo "Build configuration: cloudbuild.yaml"
echo "Region: ${GCP_REGION}"
