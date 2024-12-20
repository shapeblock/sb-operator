# First, generate a random password (30 characters)
REGISTRY_PASSWORD="e5PxZpfvklIluqy7NDUeRfNtNa"

# Generate bcrypt hash of the password
# You'll need to install htpasswd (apache2-utils)
ENCRYPTED_PASSWORD=$(htpasswd -bnBC 10 "" $REGISTRY_PASSWORD | tr -d ':\n')

# Add helm repository
helm repo add twuni https://helm.twun.io
helm repo update

# Install docker registry
helm install registry twuni/docker-registry --version 2.2.3 --namespace shapeblock \
  --set persistence.enabled=true \
  --set persistence.size=10Gi \
  --set ingress.enabled=true \
  --set "ingress.hosts[0]=registry.test1.lakshminp.xyz" \
  --set "ingress.tls[0].hosts[0]=registry.test1.lakshminp.xyz" \
  --set "ingress.tls[0].secretName=registry-tls" \
  --set "ingress.annotations.cert-manager\.io/cluster-issuer=letsencrypt-prod" \
  --set "ingress.annotations.nginx\.ingress\.kubernetes\.io/proxy-body-size=0" \
  --set "secrets.htpasswd=shapeblock:${ENCRYPTED_PASSWORD}" \
  --set updateStrategy.type=Recreate

# Create registry credentials secret
# First, create the auth string
AUTH_STRING=$(echo -n "shapeblock:${REGISTRY_PASSWORD}" | base64)

# Create the secret
kubectl create secret docker-registry registry-creds \
  --namespace shapeblock \
  --docker-server=registry.test1.lakshminp.xyz \
  --docker-username=shapeblock \
  --docker-password="${REGISTRY_PASSWORD}"

# Create kpack namespace
kubectl create namespace kpack

# Install kpack
helm repo add shapeblock https://shapeblock.github.io
helm install kpack shapeblock/sb-kpack --version 0.1.7 --namespace kpack

# Install flux2 helm operator
helm repo add fluxcd-community https://fluxcd-community.github.io/helm-charts
helm install helm-operator fluxcd-community/flux2 --version 2.13.0 --namespace shapeblock \
  --set imageAutomationController.create=false \
  --set imageReflectorController.create=false \
  --set kustomizeController.create=false

# Install operator
kubectl apply -f deployment.yaml -n shapeblock

# Install CRDs
kubectl apply -f application-crd.yaml
kubectl apply -f project-crd.yaml

# Install paketo cluster stores
kubectl apply -f paketo-artefacts.yaml

# Install helm repositories
kubectl apply -f repos.yaml
