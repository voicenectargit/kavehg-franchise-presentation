import { defineRailway, github, project, service } from "railway/iac";
// This repository manages only its own resources in the environment. Other
// repositories export their own partial name.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "content-portal";
export default defineRailway(() => {
  const contentPortal = service("content-portal", {
    source: github("voicenectargit/kavehg-franchise-presentation", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    replicas: { "us-west2": 1 },
    deploy: { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 10 },
    domains: [{ domain: "content.ambivo.com", port: 8003 }],
    networking: { privateNetworkEndpoint: "kavehg-franchise-presentation" },
  });

  return project("content-portal", {
    variables: { managed: false },
    resources: [contentPortal],
  });
});

