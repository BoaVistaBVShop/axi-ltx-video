# syntax=docker/dockerfile:1.7

ARG BASE_IMAGE="runpod/comfyui:cuda13.0@sha256:094dc6d79448b6f118c4d2b054073f92d765c568598e7a96aaeda678a6bcbf3b"
FROM ${BASE_IMAGE}

ARG COMFYUI_VERSION="v0.34.0"
ARG COMFYUI_SHA="12d5279438bfefc058a269eae805ceab6047777f"
ARG LTXVIDEO_SHA="15d09abb5a187a8dcaea2fc31fe51ee96e6c9d0d"

LABEL org.opencontainers.image.title="bvshop-ltx-video" \
      org.opencontainers.image.description="Pinned ComfyUI and LTX-2.5 runtime for disposable Runpod Pods" \
      org.opencontainers.image.source="https://github.com/BoaVistaBVShop/axi-ltx-video" \
      io.bvshop.comfyui.version="${COMFYUI_VERSION}" \
      io.bvshop.comfyui.commit="${COMFYUI_SHA}" \
      io.bvshop.ltxvideo.commit="${LTXVIDEO_SHA}"

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY scripts/pod/repair_ltx_kornia.py /opt/ltx-stack-build/repair_ltx_kornia.py

# Replace the upstream baked ComfyUI bundle while retaining the useful pinned
# Runpod nodes. Model weights are deliberately excluded from the image.
RUN --mount=type=cache,target=/root/.cache/pip \
    set -eux; \
    build_dir="$(mktemp -d)"; \
    mkdir -p "${build_dir}/ComfyUI"; \
    curl -fsSL "https://github.com/Comfy-Org/ComfyUI/archive/${COMFYUI_SHA}.tar.gz" \
      -o "${build_dir}/comfyui.tar.gz"; \
    tar -xzf "${build_dir}/comfyui.tar.gz" --strip-components=1 \
      -C "${build_dir}/ComfyUI"; \
    mkdir -p "${build_dir}/ComfyUI/custom_nodes"; \
    for node in ComfyUI-Manager ComfyUI-KJNodes Civicomfy ComfyUI-RunpodDirect; do \
      cp -a "/opt/comfyui-baked/custom_nodes/${node}" \
        "${build_dir}/ComfyUI/custom_nodes/${node}"; \
    done; \
    mkdir -p "${build_dir}/ComfyUI/custom_nodes/ComfyUI-LTXVideo"; \
    curl -fsSL "https://github.com/Lightricks/ComfyUI-LTXVideo/archive/${LTXVIDEO_SHA}.tar.gz" \
      -o "${build_dir}/ltxvideo.tar.gz"; \
    tar -xzf "${build_dir}/ltxvideo.tar.gz" --strip-components=1 \
      -C "${build_dir}/ComfyUI/custom_nodes/ComfyUI-LTXVideo"; \
    python3.12 /opt/ltx-stack-build/repair_ltx_kornia.py \
      --path "${build_dir}/ComfyUI/custom_nodes/ComfyUI-LTXVideo/pyramid_blending.py"; \
    PIP_CONSTRAINT=/opt/comfyui-runtime-constraints.txt \
      python3.12 -m pip install \
      -r "${build_dir}/ComfyUI/requirements.txt" \
      -r "${build_dir}/ComfyUI/custom_nodes/ComfyUI-LTXVideo/requirements.txt"; \
    printf '%s\n' \
      "COMFYUI_VERSION=${COMFYUI_VERSION}" \
      "COMFYUI_SHA=${COMFYUI_SHA}" \
      "LTXVIDEO_SHA=${LTXVIDEO_SHA}" \
      > "${build_dir}/ComfyUI/.runpod-bundle-version"; \
    rm -rf /opt/comfyui-baked; \
    mv "${build_dir}/ComfyUI" /opt/comfyui-baked; \
    rm -rf "${build_dir}"; \
    sed -i \
      's/BAKED_NODES=("ComfyUI-Manager" "ComfyUI-KJNodes" "Civicomfy" "ComfyUI-RunpodDirect")/BAKED_NODES=("ComfyUI-Manager" "ComfyUI-KJNodes" "Civicomfy" "ComfyUI-RunpodDirect" "ComfyUI-LTXVideo")/' \
      /start.sh; \
    sed -i \
      's/BAKED_NODES="ComfyUI-Manager ComfyUI-KJNodes Civicomfy ComfyUI-RunpodDirect"/BAKED_NODES="ComfyUI-Manager ComfyUI-KJNodes Civicomfy ComfyUI-RunpodDirect ComfyUI-LTXVideo"/' \
      /start.sh; \
    grep -q 'ComfyUI-LTXVideo' /start.sh

COPY docker/extra_model_paths.yaml /opt/comfyui-baked/extra_model_paths.yaml
COPY docker/healthcheck.sh /usr/local/bin/ltx-healthcheck
COPY config/models-manifest.json /opt/ltx-stack/models-manifest.json
COPY config/workflows-manifest.json /opt/ltx-stack/workflows-manifest.json
COPY config/generation-profiles.json /opt/ltx-stack/generation-profiles.json
COPY scripts/pod/download_models.py /opt/ltx-stack/download_models.py
COPY scripts/pod/download_workflows.py /opt/ltx-stack/download_workflows.py
COPY scripts/pod/finalize_output.py /opt/ltx-stack/finalize_output.py
COPY scripts/pod/bootstrap.py /opt/ltx-stack/bootstrap.py

RUN chmod 0755 /usr/local/bin/ltx-healthcheck \
    /opt/ltx-stack/download_models.py \
    /opt/ltx-stack/download_workflows.py \
    /opt/ltx-stack/finalize_output.py \
    /opt/ltx-stack/bootstrap.py \
    && python3.12 -m py_compile \
    /opt/ltx-stack/download_models.py \
    /opt/ltx-stack/download_workflows.py \
    /opt/ltx-stack/finalize_output.py \
    /opt/ltx-stack/bootstrap.py

HEALTHCHECK --interval=30s --timeout=5s --start-period=5m --retries=3 \
  CMD ["/usr/local/bin/ltx-healthcheck"]
