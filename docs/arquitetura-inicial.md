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
| Entrega | Network Volume → API S3 Runpod → `.incoming` local → validação → `entregas/<job-id>` |

O `EU-RO-1` foi escolhido porque, no catálogo consultado, oferece volume `STANDARD` e aparece na disponibilidade tanto da RTX 5090 quanto da RTX PRO 6000. Isso permite que prévias e finais usem o mesmo volume, sem duplicar ou migrar os modelos.

## Como a operação fica depois da configuração única

1. `iniciar`: criar um Pod descartável pelo template e anexar o volume existente;
2. `gerar`: carregar o job, produzir A/B, validar e mover os arquivos completos para `ready`;
3. `receber`: terminar o Pod, baixar pela API S3, conferir SHA-256 e FFprobe;
4. `vazar`: confirmar a entrega local e garantir que não existe GPU ou Pod órfão cobrando.

O que permanece entre sessões é a imagem versionada, o template e o network volume. O Pod não permanece.

## Inventário de modelos e espaço

Tamanhos obtidos dos arquivos oficiais do LTX-2.5 e convertidos para planejamento:

| Pacote | Tamanho aproximado |
|---|---:|
| Prévia INT8 mínima, sem prompt enhancer | 39,71 GB |
| Prévia INT8 completa | 44,91 GB |
| Prévia INT8 + refinamento | 45,50 GB |
| INT8 completo + transformer dev INT8 | 67,01 GB |
| INT8 completo + conjunto final distilled BF16 | 113,79 GB |

Por isso:

- 100 GB é apertado para software, caches e outputs;
- 150 GB serve para uma implantação inicialmente concentrada em INT8, mas deixa pouca margem se o BF16 for adicionado;
- 200 GB comporta prévia INT8, final BF16, ComfyUI, caches e uma margem operacional coerente com a recomendação oficial de 200 GB de SSD do LTX-2.5.

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

O build foi preparado em `.github/workflows/build-image.yml` para ocorrer no GitHub Actions e publicar no GitHub Container Registry (GHCR). Isso evita consumir dezenas de gigabytes no Docker Desktop local. O workflow é somente manual, fixa as actions por commit, gera SBOM/proveniência e registra o digest imutável. Ele não será executado até existir um repositório GitHub e o usuário autorizar a publicação.

Como a imagem não contém pesos, referências do cliente nem segredos, a opção operacional mais simples é torná-la pública no GHCR. Se o usuário preferir mantê-la privada, será necessário cadastrar autenticação de container registry na Runpod; isso é independente das credenciais S3 já configuradas.

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

O custo real por cena ainda não pode ser estimado com honestidade sem um benchmark representativo de duração, resolução, FPS e workflow. O primeiro Pod deverá ter `terminate-after` rígido e um teto explícito aprovado pelo usuário.

## Próximas dependências antes de provisionar

1. aceitar a licença do `Lightricks/LTX-2.5` na Hugging Face e configurar um token de leitura sem enviá-lo pelo chat;
2. criar/escolher o repositório GitHub e decidir se o pacote GHCR será público ou privado;
3. confirmar o orçamento persistente do volume de 200 GB e o teto do piloto;
4. fornecer duas cenas/referências representativas e os parâmetros de entrega;
5. então construir/publicar a imagem, criar volume/template, carregar os modelos uma vez e executar dois testes de recriação.

## Fontes primárias

- LTX-2.5: <https://huggingface.co/Lightricks/LTX-2.5>
- Repositório LTX-2: <https://github.com/Lightricks/LTX-2>
- Requisitos de sistema: <https://docs.ltx.io/open-source-model/getting-started/system-requirements>
- Integração ComfyUI: <https://docs.ltx.io/open-source-model/integration-tools/comfy-ui>
- Workflows LTX-2.5: <https://github.com/Lightricks/ComfyUI-LTXVideo/tree/master/example_workflows/2.5>
- Preços Runpod: <https://www.runpod.io/pricing>
