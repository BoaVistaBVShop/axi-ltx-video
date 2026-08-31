# AXI LTX Video

Infraestrutura reproduzível para geração de vídeo com LTX-2.5 e ComfyUI em Pods descartáveis da Runpod.

O projeto separa três responsabilidades:

- a imagem Docker contém somente o runtime e as dependências fixadas;
- os modelos ficam em um network volume persistente;
- cada sessão usa um Pod temporário, eliminado ao terminar a fila.

Nenhuma credencial, peso de modelo ou material de cliente é incluído na imagem ou neste repositório.

## Stack fixada

- base: `runpod/comfyui:cuda13.0` por digest;
- plataforma: `linux/amd64`;
- ComfyUI: `v0.34.0` / commit fixado;
- `ComfyUI-LTXVideo`: commit fixado;
- modelos: LTX-2.5 com tamanho e SHA-256 registrados em `config/models-manifest.json`.

## Build da imagem

O workflow `Build pinned LTX image` é manual. No GitHub:

1. abra **Actions**;
2. selecione **Build pinned LTX image**;
3. execute com uma tag semântica, por exemplo `v0.1.0`;
4. registre o digest retornado no resumo do job.

A imagem é publicada como:

```text
ghcr.io/boavistabvshop/axi-ltx-video:<tag>
```

O template Runpod deve usar o digest imutável produzido pelo build, não uma tag flutuante.

## Modelos no volume

Depois de aceitar a licença do modelo na Hugging Face e configurar `HF_TOKEN` como segredo no Pod:

```bash
python /opt/ltx-stack/download_models.py --profile preview
```

Perfis disponíveis:

- `preview`: distilled INT8 ConvRot;
- `final-int8`: conjunto INT8 para finalização econômica;
- `final-bf16`: componentes BF16 para a GPU final de maior VRAM.

Cada arquivo é validado por tamanho e SHA-256 antes de ser considerado pronto.

## Documentação

- `docs/arquitetura-inicial.md`: arquitetura, custos e decisões;
- `docs/parametros-ltx.md`: parâmetros criativos e perfis técnicos;
- `config/stack.json`: configuração planejada da Runpod;
- `config/generation-profiles.json`: presets de prévia e final;
- `AGENTS.md`: regras operacionais e estado completo do projeto.

Nenhum recurso cobrável da Runpod deve ser criado sem aprovação explícita do orçamento correspondente.
