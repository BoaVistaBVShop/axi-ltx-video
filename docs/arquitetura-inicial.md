# Arquitetura inicial recomendada — Runpod + LTX-2.5

Snapshot técnico: 2026-08-31. Nenhum recurso cobrável foi criado durante esta análise.

## Decisão recomendada

| Camada | Configuração |
|---|---|
| Cloud | Runpod Secure Cloud |
| Data center | `EU-RO-1` |
| Volume persistente | Network Volume `STANDARD`, 200 GB, montado em `/workspace` |
| GPU de prévia | `NVIDIA GeForce RTX 5090`, 32 GB, US$ 0,99/h no snapshot |
| GPU de final | `NVIDIA RTX PRO 6000 Blackwell Server Edition`, 96 GB, US$ 2,09/h no snapshot |
| CUDA | 13.0 |
| Base da imagem | `runpod/comfyui:cuda13.0@sha256:094dc6d79448b6f118c4d2b054073f92d765c568598e7a96aaeda678a6bcbf3b` |
| ComfyUI | `v0.34.0`, fixado por versão/commit |
| Custom node LTX | `Lightricks/ComfyUI-LTXVideo` no commit `15d09abb5a18` |
| Modelo de prévia | LTX-2.5 distilled INT8 ConvRot, workflow oficial single-stage |
| Modelo de final | primeiro benchmark: distilled INT8 two-stage; opção de qualidade máxima: distilled BF16 two-stage na GPU de 96 GB |
| Acesso ao Pod | somente SSH em `22/tcp`; ComfyUI via port forwarding local |
| Entrega | Network Volume → API S3 Runpod → `.incoming` local → validação → `entregas/<job-id>` |

O `EU-RO-1` foi escolhido porque, no catálogo consultado, oferece volume `STANDARD` e aparece na disponibilidade tanto da RTX 5090 quanto da RTX PRO 6000. Isso permite que prévias e finais usem o mesmo volume, sem duplicar ou migrar os modelos.

## Como a operação fica depois da configuração única

1. `iniciar`: criar um Pod descartável pelo template e anexar o volume existente;
2. `gerar`: carregar o job, produzir A/B, validar e mover os arquivos completos para `ready`;
3. `receber`: terminar o Pod, baixar pela API S3, conferir SHA-256 e FFprobe;
4. `vazar`: confirmar a entrega local e garantir que não existe GPU ou Pod órfão cobrando.

O que permanece entre sessões é a imagem versionada, o template e o network volume. O Pod não permanece.

## Canal de configuração e operação: SSH obrigatório

O Pod é criado e excluído pelo plano de controle da Runpod (`runpodctl`, MCP ou API). Depois que o acesso estiver disponível, toda ação dentro dele será conduzida por SSH a partir do ambiente local: bootstrap inicial, preparação do volume, download e validação dos modelos, health checks, smoke test, envio do manifest, início e acompanhamento da geração, validação dos outputs e diagnóstico.

Não haverá configuração por Web Terminal, Jupyter, shell do navegador ou cliques no ComfyUI Manager. As rotinas SSH devem chamar scripts versionados e idempotentes: na primeira execução preparam apenas o que falta; nas seguintes validam o estado existente e seguem diretamente para o job. Se uma correção for descoberta durante uma sessão, ela deve ser incorporada à imagem, ao template ou aos scripts e comprovada em um Pod novo.

O fluxo técnico fica: `provisionar → aguardar SSH → bootstrap/validação idempotente → gerar → publicar em ready → excluir Pod → receber por S3 → validar localmente`. O S3 continua sendo o canal principal de entrega; SSH é o canal obrigatório de controle do Pod. O Pod deve ser excluído assim que os outputs estiverem seguros no network volume, sem aguardar o download local.

## Inventário de modelos e espaço

Tamanhos obtidos dos arquivos oficiais do LTX-2.5 e convertidos para planejamento:

| Pacote | Tamanho aproximado |
|---|---:|
| Prévia INT8 mínima, sem prompt enhancer | 39,71 GB |
| Prévia INT8 completa | 44,91 GB |
| Prévia INT8 + refinamento | 45,50 GB |
| INT8 completo + transformer dev INT8 | 67,01 GB |
| INT8 completo + conjunto final distilled BF16 | 113,79 GB |
| Quatro IC-LoRAs opcionais configurados | 3,60 GB |
| Total de pesos configurados | aproximadamente 117,39 GB |

Por isso:

- 100 GB é apertado para software, caches e outputs;
- 150 GB serve para uma implantação inicialmente concentrada em INT8, mas deixa pouca margem se o BF16 for adicionado;
- 200 GB comporta prévia INT8, final BF16, os quatro IC-LoRAs, ComfyUI, caches e uma margem operacional coerente com a recomendação oficial de 200 GB de SSD do LTX-2.5.

Na tarifa de referência de US$ 0,07/GB/mês, 200 GB equivalem a aproximadamente US$ 14/mês, cobrados proporcionalmente enquanto o volume existir. Com o saldo observado de US$ 10, o volume não deve ser criado sem decisão explícita sobre recarga e duração do piloto.

O volume pode crescer depois, mas não deve ser tratado como temporário: ele é o principal custo persistente e continua cobrando mesmo sem Pod.

## Estratégia de imagem

A tag pública `runpod/comfyui:cuda13.0` será usada apenas como origem. A imagem do projeto deve referenciar o digest registrado acima e fixar também:

- ComfyUI `v0.34.0`;
- `ComfyUI-LTXVideo` no commit `15d09abb5a18`;
- FFmpeg/FFprobe e dependências do workflow;
- scripts de readiness, execução, validação, download e teardown;
- versões/hashes em um manifest legível por máquina.

Os pesos não entram na imagem: ficam no network volume. Assim, atualizar a automação não obriga a reenviar mais de 100 GB de modelos ao registry, e recriar um Pod não baixa tudo novamente.

O build em `.github/workflows/build-image.yml` ocorre no GitHub Actions e publica no GitHub Container Registry (GHCR), evitando consumir dezenas de gigabytes no Docker Desktop local. A imagem vigente `v0.2.0` e seu digest imutável estão em `config/stack.json`; o workflow continua manual, com actions fixadas por commit, SBOM e proveniência.

Por decisão explícita mais recente do usuário, o pacote `axi-ltx-video` é público no GHCR. A política da organização foi atualizada com autorização específica e o acesso anônimo à tag e ao digest vigentes foi validado. O template Runpod poderá obter a imagem sem credencial de registry. Pesos gated, tokens, credenciais S3 e materiais de cliente permanecem fora da imagem.

A variante NVFP4 não será a base inicial. A integração LTX-2.5/ComfyUI ainda é recente e há relatos oficiais recentes de incompatibilidades específicas; o primeiro caminho deve reproduzir o workflow INT8 ConvRot oficial em Linux. Depois do smoke test, BF16 e NVFP4 podem ser comparados por qualidade, memória, tempo e estabilidade.

## Estrutura persistente proposta

```text
/workspace/
├── models/
│   ├── diffusion_models/
│   ├── text_encoders/
│   ├── vae/
│   ├── latent_upscale_models/
│   └── loras/
├── workflows/
├── inputs/
├── jobs/
│   └── <job-id>/
│       ├── manifest.json
│       ├── output-staging/
│       ├── ready/
│       ├── checksums.sha256
│       └── qc-report.json
└── cache/
```

## Custos de referência do piloto

Além do volume, a GPU é cobrada apenas enquanto o Pod existir:

- RTX 5090 Secure: US$ 0,99/h;
- RTX PRO 6000 Secure: US$ 2,09/h.

O custo real por cena ainda não pode ser estimado com honestidade sem um benchmark representativo de duração, resolução, FPS e workflow. O primeiro Pod deverá ter deadline rígido e um teto explícito aprovado pelo usuário. Como a ajuda ao vivo do `runpodctl` 2.12.0 não expõe `--terminate-after`, o limite deve ser garantido por watchdog externo que exclua o Pod pelo plano de controle; uma expiração nativa é apenas uma proteção adicional quando a ferramenta/API em uso confirmar suporte.

## Próximas dependências antes de provisionar

1. confirmar o orçamento persistente do volume de 200 GB e o teto do piloto;
2. fornecer duas cenas/referências representativas e os parâmetros de entrega;
3. então criar volume/template, testar SSH, carregar os modelos uma vez e executar dois testes de recriação.

## Fontes primárias

- LTX-2.5: <https://huggingface.co/Lightricks/LTX-2.5>
- Repositório LTX-2: <https://github.com/Lightricks/LTX-2>
- Requisitos de sistema: <https://docs.ltx.io/open-source-model/getting-started/system-requirements>
- Integração ComfyUI: <https://docs.ltx.io/open-source-model/integration-tools/comfy-ui>
- Workflows LTX-2.5: <https://github.com/Lightricks/ComfyUI-LTXVideo/tree/master/example_workflows/2.5>
- Preços Runpod: <https://www.runpod.io/pricing>
