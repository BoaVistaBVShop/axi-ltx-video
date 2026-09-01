# AXI LTX Video

Infraestrutura reproduzível para geração de vídeo com LTX-2.5 e ComfyUI em Pods descartáveis da Runpod.

O projeto separa três responsabilidades:

- a imagem Docker contém somente o runtime e as dependências fixadas;
- os modelos ficam em um network volume persistente;
- cada sessão usa um Pod temporário, eliminado ao terminar a fila.
- toda configuração e operação interna do Pod é feita via SSH por scripts versionados e idempotentes; interfaces web não são usadas para instalar ou corrigir o ambiente;
- o fluxo normal é `provisionar → SSH → gerar → validar em ready → excluir Pod → baixar via S3 → validar localmente`.

Nenhuma credencial, peso de modelo ou material de cliente é incluído na imagem ou neste repositório.

## Stack fixada

- base: `runpod/comfyui:cuda13.0` por digest;
- plataforma: `linux/amd64`;
- ComfyUI: `v0.34.0` / commit fixado;
- `ComfyUI-LTXVideo`: commit fixado;
- modelos e IC-LoRAs com tamanho e SHA-256 registrados em `config/models-manifest.json`;
- workflows oficiais fixados por commit e SHA-256 em `config/workflows-manifest.json`.

## Build da imagem

O workflow `Build pinned LTX image` é manual. No GitHub:

1. abra **Actions**;
2. selecione **Build pinned LTX image**;
3. execute com uma nova tag semântica, por exemplo `v0.3.0`;
4. registre o digest retornado no resumo do job.

A imagem é publicada como:

```text
ghcr.io/boavistabvshop/axi-ltx-video:<tag>
```

O template Runpod deve usar o digest imutável produzido pelo build, não uma tag flutuante.

## Operação local por SSH

Valide a estação local sem criar recursos:

```powershell
.\scripts\local\axi-ltx.ps1 doctor
```

A CLI oferece comandos fechados para readiness, bootstrap idempotente, túnel
local do ComfyUI, submissão/retomada, publicação em `ready`, watchdog e teardown.
O contrato completo e os exemplos estão em `docs/orquestracao-ssh.md`.

## Modelos no volume

Depois de aceitar a licença do modelo na Hugging Face e configurar `HF_TOKEN` como segredo no Pod:

```bash
python /opt/ltx-stack/download_models.py --profile preview
```

Esse comando será chamado pelo bootstrap versionado através de SSH. Ele não deve ser executado manualmente pelo Web Terminal, Jupyter ou ComfyUI Manager.

Perfis disponíveis:

- `preview`: distilled INT8 ConvRot;
- `final-int8`: conjunto INT8 para finalização econômica;
- `final-bf16`: componentes BF16 para a GPU final de maior VRAM.
- `baseline-bf16-single-stage`: baseline comparável aos IC-LoRAs single-stage;
- `lora-ingredients-bf16`: consistência por folha de referências;
- `lora-motion-track-bf16`: controle por trajetórias;
- `lora-union-control-bf16`: controle estrutural combinado;
- `lora-outpaint-bf16`: inpainting/outpainting com vídeo e máscara.

Cada arquivo é validado por tamanho e SHA-256 antes de ser considerado pronto.
O workflow correspondente é preparado pelo mesmo perfil:

```bash
python /opt/ltx-stack/download_models.py --profile lora-ingredients-bf16
python /opt/ltx-stack/download_workflows.py --profile lora-ingredients-bf16
```

Os perfis LoRA não são ativados automaticamente. Antes do envio de cada cena, o
operador deve perguntar se haverá A/B baseline versus LoRA e registrar a resposta
no manifest do job. Os workflows oficiais configurados usam BF16 e começam na
RTX PRO 6000; a combinação INT8+LoRA permanece desabilitada até benchmark real.

## Documentação

- `docs/arquitetura-inicial.md`: arquitetura, custos e decisões;
- `docs/parametros-ltx.md`: parâmetros criativos e perfis técnicos;
- `docs/orquestracao-ssh.md`: CLI local, segurança SSH, watchdog e recuperação;
- `config/stack.json`: configuração planejada da Runpod;
- `config/generation-profiles.json`: presets de prévia e final;
- `config/workflows-manifest.json`: origem, versão e integridade dos workflows;
- `AGENTS.md`: regras operacionais e estado completo do projeto.

Nenhum recurso cobrável da Runpod deve ser criado sem aprovação explícita do orçamento correspondente.
