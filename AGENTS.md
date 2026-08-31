# AGENTS.md — Operação de vídeo com Runpod e LTX

## 1. Escopo e objetivo do projeto

Este workspace existe para produzir e editar vídeos com aceleração por GPU na Runpod, usando a família LTX e workflows automatizados. O objetivo permanente é combinar:

1. qualidade visual máxima e consistência entre cenas;
2. alta economia de GPU e armazenamento;
3. execução rápida por lotes, evitando retrabalho;
4. resultados reproduzíveis, auditáveis e fáceis de retomar;
5. uma única configuração estrutural do ambiente, seguida de sessões simples de iniciar, gerar, baixar e encerrar.

O usuário não quer reinstalar ou reconfigurar manualmente o ambiente a cada trabalho. Todo desenho técnico deve preservar esse requisito.

## 2. Decisão arquitetural já aprovada

A arquitetura-alvo é formada por três artefatos persistentes e Pods descartáveis:

1. **Imagem Docker imutável e versionada:** contém sistema operacional, CUDA/PyTorch compatíveis, ComfyUI, FFmpeg, runtime LTX, custom nodes, dependências e scripts de automação. Dependências devem ter versões fixadas. Sempre que possível, registrar também hashes dos artefatos.
2. **Template Runpod versionado:** define imagem, portas, tamanho de container disk, comando de inicialização, variáveis não secretas, limites e configuração de montagem.
3. **Network volume persistente:** contém pesos grandes de modelos, text encoders, VAEs, workflows, cache reutilizável, referências, manifests de jobs e resultados temporários que precisem sobreviver ao Pod.

Os Pods de renderização são efêmeros. Em cada sessão deve ser criado um Pod novo a partir da imagem e do template, anexando o mesmo network volume. Ao final, o Pod deve ser terminado/excluído. Para o usuário, a automação deve apresentar isso como uma operação simples de “ligar” e “desligar”, mesmo que tecnicamente seja criação e exclusão.

Não depender de instalações ou alterações manuais feitas apenas dentro de um Pod. Tudo que define o ambiente deve estar na imagem, no template, no volume ou em manifests versionados.

## 3. Por que não manter um único Pod parado

- O container disk é efêmero e seu conteúdo é apagado ao parar/reiniciar ou resetar o Pod.
- O volume disk local preserva `/workspace` durante stop/start, mas é apagado quando o Pod é terminado e continua gerando cobrança enquanto o Pod está parado.
- A cobrança documentada para volume disk parado é maior que a do mesmo volume durante execução. Verificar sempre a tarifa atual antes de tomar decisões.
- Parar libera a GPU; reiniciar depois não garante que a mesma GPU, ou qualquer GPU, estará disponível.
- Pods anexados a network volumes atualmente não podem ser parados; precisam ser terminados. O network volume sobrevive independentemente.
- Portanto, o fluxo padrão deste projeto é **create → render → download/backup → delete**, e não stop/start.

Só reconsiderar manter um Pod parado se houver sessões muito próximas, uma medição real demonstrar benefício e o usuário aprovar explicitamente o custo de armazenamento e o risco operacional.

## 4. Fluxo único de configuração inicial

Antes de criar recursos cobrados:

1. verificar os modelos LTX atuais, licenças, requisitos de VRAM, workflows oficiais e compatibilidade real com ComfyUI;
2. verificar preços e disponibilidade atuais de GPUs e data centers na conta Runpod;
3. inventariar exatamente todos os pesos, encoders, VAEs, caches, custom nodes e margem necessária para outputs;
4. escolher o tamanho do network volume com base nesse inventário, nunca por palpite;
5. escolher o data center do volume considerando a disponibilidade das GPUs desejadas, pois o volume fica preso ao data center;
6. decidir Standard versus High-Performance com base em benchmark e perfil de I/O; usar Standard como hipótese inicial, não como decisão cega;
7. construir a imagem Docker reproduzível e publicá-la em um registry apropriado;
8. criar o template Runpod;
9. carregar os modelos no volume apenas uma vez;
10. executar um smoke test real de ponta a ponta;
11. baixar e validar o artefato de teste;
12. terminar o Pod de configuração/teste e confirmar que imagem, template e volume bastam para recriá-lo.

O teste de aceitação da configuração inicial é: criar um Pod do zero usando somente os artefatos persistentes, gerar uma pequena cena válida, obter o resultado fora do Pod e excluir o Pod sem perder modelos, workflows ou configuração.

## 5. Fluxo normal de cada workload

1. Receber cenas, imagens ou vídeos de referência e o briefing.
2. Registrar duração, proporção, resolução, FPS, requisitos de áudio, estilo, continuidade, privacidade e formato de entrega.
3. Montar um manifest de job reproduzível.
4. Estimar GPU, tempo, armazenamento temporário e custo máximo.
5. Obter aprovação explícita antes de criar qualquer recurso cobrado quando ainda não houver autorização para aquele lote.
6. Criar o Pod pelo template, no mesmo data center do network volume.
7. Aplicar proteção de tempo máximo já na criação do Pod.
8. Aguardar readiness real do serviço e verificar ComfyUI/runtime LTX com uma chamada de saúde, não apenas pelo status `RUNNING`.
9. Enviar ou referenciar o workload.
10. Gerar, por padrão, duas variações A/B por cena na mesma inicialização e com seeds registradas.
11. Apresentar as prévias e renderizar em qualidade final somente as versões aprovadas.
12. Fazer upscale e pós-processamento apenas quando agregarem qualidade à versão escolhida.
13. Executar controle de qualidade técnico e visual.
14. Baixar/backup dos resultados, manifests e metadados importantes.
15. Remover intermediários descartáveis.
16. Terminar/excluir o Pod assim que a fila acabar.
17. Registrar tempo, custo, GPU, modelo, workflow, seed e resultado para melhorar estimativas futuras.

Agrupar cenas em workloads é a estratégia padrão. Não ligar e desligar uma GPU para cada cena isoladamente quando várias cenas já estiverem disponíveis, pois isso repete pull da imagem, boot e carregamento dos modelos.

### 5.1. Arquitetura aprovada de recebimento dos vídeos

O usuário aprovou **Secure Cloud + network volume + download local pela API S3 compatível da Runpod** como canal principal de recebimento.

Fluxo obrigatório:

1. o Pod renderiza em `/workspace/jobs/<job-id>/output-staging`;
2. o arquivo é fechado e validado com FFprobe;
3. são registrados tamanho, duração, streams, codec, resolução, FPS e SHA-256;
4. somente um arquivo válido é movido para `/workspace/jobs/<job-id>/ready`;
5. após todos os outputs necessários estarem seguros em `ready`, o Pod pode ser terminado para interromper cobrança de GPU;
6. um downloader local acessa o network volume pela API S3, sem iniciar outro Pod;
7. o download entra primeiro em uma pasta local `.incoming`;
8. SHA-256 e FFprobe são verificados novamente localmente;
9. somente depois da validação o job é movido para a pasta local `entregas/<job-id>`;
10. os arquivos remotos só podem ser limpos após confirmação de recebimento local e backup.

Estados mínimos do job: `rendering → validating → ready → downloading → received → archived/purged`.

Estrutura local sugerida:

```text
entregas/<job-id>/
├── previews/
├── finais/
├── masters/
├── thumbnails/
├── manifest.json
├── checksums.sha256
└── qc-report.json
```

Entregar por padrão duas cópias quando aplicável: MP4 de visualização e master de edição (ProRes 422 HQ ou DNxHR HQX, a confirmar conforme o editor do usuário). `runpodctl receive`, SCP e rsync são canais de contingência, não o caminho principal automatizado.

O data center escolhido para o network volume deve oferecer a API S3 da Runpod e as GPUs desejadas. Confirmar a lista atual antes de criar o volume. Network volumes para Pods estão atualmente associados ao Secure Cloud; revalidar essa regra na documentação no momento do provisionamento.

## 6. Estratégia de geração LTX

- Usar o melhor modelo LTX disponível e tecnicamente adequado no momento da execução. A conversa anterior adotou **LTX-2.5** como referência, mas o agente deve confirmar que ele ainda é atual, licenciado e compatível antes do primeiro provisionamento.
- Para prévias, priorizar uma variante distilled/quantizada que preserve qualidade suficiente e reduza tempo e custo.
- Gerar duas versões A/B por cena sempre que isso reduzir a chance de religar GPU por retrabalho.
- Usar resolução moderada nas prévias, mantendo composição, movimento e timing representativos.
- Registrar seed, prompt, negative prompt, modelo, pesos auxiliares, workflow, resolução, FPS, duração e parâmetros de sampling.
- Renderizar a versão final somente depois de selecionar a melhor prévia, salvo quando o usuário autorizar geração final direta.
- Reservar modelo full/SFT e GPUs mais caras para cenas principais ou problemas que realmente demonstrem ganho de qualidade.
- Preservar continuidade de personagens, objetos, cenário, iluminação, câmera, figurino, identidade e direção de movimento entre cenas.
- Se houver geração de áudio, validar sincronização audiovisual e nível/qualidade do áudio separadamente.

O controle de qualidade deve verificar pelo menos: aderência ao briefing, composição, anatomia, identidade, estabilidade temporal, flicker, deformações, objetos surgindo/desaparecendo, continuidade, motion blur, câmera, resolução real, duração, FPS, áudio e artefatos de compressão.

## 7. Hipóteses históricas de GPU — sempre revalidar

A proposta inicial da conversa foi:

- prévia: LTX distilled INT8 em RTX 5090 32 GB;
- final padrão: LTX distilled BF16 em RTX PRO 6000 96 GB ou A100 80 GB;
- cena principal excepcional: modelo full/SFT em H200 141 GB.

Preços observados anteriormente, apenas como registro histórico e não como cotação válida:

- RTX 5090: US$ 0,69/h Community ou US$ 0,99/h Secure;
- A100 SXM 80 GB: US$ 1,39/h Community ou US$ 1,59/h Secure;
- RTX PRO 6000 96 GB: US$ 1,69/h Community ou US$ 2,09/h Secure;
- H200 141 GB: US$ 3,59/h Community ou US$ 4,59/h Secure.

Esses valores e a própria disponibilidade podem mudar. Consultar o catálogo ao vivo da conta antes de cada provisionamento. Não escolher GPU somente por nome ou preço: confirmar VRAM, compatibilidade, velocidade medida, disponibilidade no data center do volume e custo por segundo aprovado de vídeo.

Para material não confidencial, Community Cloud pode ser usada para maximizar economia quando confiabilidade e disponibilidade forem aceitáveis. Para material confidencial de cliente, preferir Secure Cloud e confirmar a classificação com o usuário.

## 8. Regras obrigatórias de custo

- Nenhum Pod, volume, endpoint ou outro recurso cobrado deve ser criado sem autorização explícita do usuário quando a autorização do lote ainda estiver pendente.
- Antes da primeira cobrança, apresentar configuração, custo horário, estimativa do lote, custo persistente e limite máximo.
- Usar um limite rígido de tempo/`terminate-after` como proteção contra esquecimento.
- Manter um watchdog de inatividade; a hipótese anterior foi terminar após aproximadamente 10 minutos sem trabalho, mas o encerramento imediato ao fim da fila é preferível quando todos os outputs já estiverem seguros.
- Terminar/excluir o Pod, não apenas pará-lo, no fluxo padrão.
- Não manter GPU ociosa enquanto se espera briefing, escolha criativa ou envio de arquivos.
- Gerar A/B na mesma carga do modelo quando isso reduzir retrabalho.
- Fazer upscale e render final somente nas versões selecionadas.
- Limpar outputs temporários e caches que possam ser recriados, sem apagar pesos ou workflows persistentes.
- Registrar custo total, custo por cena e custo por segundo aprovado.
- O spend limit geral da conta não substitui um limite específico do workload.
- O network volume gera cobrança enquanto existir, mesmo sem Pod. Revisar periodicamente ocupação e necessidade.

O inventário técnico de 2026-08-31 levou à recomendação de um network volume `STANDARD` de 200 GB, estimado em cerca de US$ 14/mês na tarifa de referência de US$ 0,07/GB/mês. **Isso não foi autorizado e não deve ser criado automaticamente.** A recomendação e os cálculos estão em `docs/arquitetura-inicial.md`.

O piloto sugerido anteriormente tinha limite de US$ 5, duas cenas, duas prévias por cena e uma final selecionada de cada. Esse limite ainda precisa de confirmação explícita antes de provisionar recursos.

## 9. Segurança, credenciais e privacidade

- Nunca pedir ao usuário que envie API keys, tokens ou senhas pelo chat.
- Nunca imprimir credenciais em logs, respostas, commits, manifests ou arquivos deste workspace.
- Nunca gravar credenciais dentro da imagem Docker ou do `AGENTS.md`.
- Usar variáveis de ambiente, secrets do Runpod, arquivos de configuração fora do repositório ou mecanismos do registry.
- A `RUNPOD_API_KEY` já foi configurada localmente pelo usuário por meio de `runpodctl doctor`; não pedir a chave novamente sem evidência de que expirou ou foi revogada.
- Token da Hugging Face, se necessário, deve ser configurado diretamente como segredo. A aceitação da licença do modelo deve ser verificada separadamente.
- Chaves S3 da Runpod são distintas da API key de infraestrutura. Só criar/configurar S3 se o fluxo realmente usar a API compatível com S3.
- O usuário informou que as credenciais S3 da Runpod já foram criadas no Console. Não criar outra chave sem necessidade. A configuração local ainda deverá ser feita diretamente pelo usuário, sem enviar o segredo pelo chat.
- Não reutilizar a API key Runpod como credencial de Hugging Face, Docker, GitHub, S3 ou qualquer outro serviço.
- Não incluir dados pessoais da conta em documentação ou relatórios desnecessários.
- Material de cliente confidencial não deve ser colocado em Community Cloud sem autorização consciente do usuário.
- Backups importantes devem existir fora da Runpod; a Runpod não deve ser tratada como armazenamento permanente de longo prazo.

## 10. Estado técnico verificado em 2026-08-31

- Sistema local: Windows com PowerShell.
- Workspace: `C:\Users\Guilherme\dev\freelancers\2025\acao_conecta\bvshop\edição videos big one`.
- Plugin oficial Runpod `runpod@runpod` versão 1.2.0 foi instalado/habilitado na configuração anterior.
- O conjunto de oito skills Runpod estava instalado: router principal e lanes auxiliares, incluindo `runpod-migrate` e `runpod-templates`.
- MCP hospedado configurado em `https://mcp.getrunpod.io/` e autenticado via OAuth.
- O OAuth do MCP autentica apenas o MCP; ele não substitui a API key necessária para operações CLI como transferência de arquivos, SSH, Hub e automações locais.
- `runpodctl` 2.12.0 foi instalado em `C:\Users\Guilherme\AppData\Local\Programs\runpodctl\runpodctl.exe`.
- O SHA-256 do binário foi validado contra o checksum publicado na release oficial e o diretório foi adicionado ao PATH do usuário.
- A API key foi configurada pelo usuário diretamente no terminal, sem passar pelo chat.
- `runpodctl user` respondeu com sucesso.
- Snapshot observado da conta nessa data: saldo de US$ 10, gasto corrente de US$ 0/h e spend limit geral de US$ 80/h. Esses números são temporários e devem ser consultados novamente antes de qualquer custo.
- `runpodctl pod list --all` retornou lista vazia.
- Não havia Pods ativos nem recursos computacionais cobrados criados por esta conversa.
- `flash` não foi instalado porque ainda não é necessário para o fluxo inicial baseado em Pods. Só instalar se uma futura etapa serverless/code-first justificar.
- O usuário informou que já criou credenciais S3 no Console Runpod.
- Docker Desktop com Docker Engine 29.7.2 e backend Linux/amd64 foi validado como operacional após a atualização do WSL. A versão do Docker Desktop observada antes dessa atualização foi 4.84.0; consultar novamente quando a versão exata importar.
- O WSL foi atualizado de 2.1.5 para 2.7.12 para corrigir `WSL/Service/E_UNEXPECTED` no primeiro boot de uma distribuição normal.
- Ubuntu 26.04.1 LTS foi instalado no WSL2 e inicializado como ambiente dedicado de automação.
- AWS CLI 2.36.35 foi instalado dentro do Ubuntu/WSL e validado com `aws --version`.
- O perfil local `runpod-s3` foi configurado pelo usuário diretamente no prompt interativo. A validação confirmou apenas os prefixos esperados (`user_...` e `rps_...`) sem exibir valores; `/root/.aws/credentials` e `/root/.aws/config` ficaram com permissão `600`.
- `scripts/verify_runpod_s3_profile.sh` verifica com segurança a presença, os formatos e as permissões do perfil sem imprimir as credenciais.
- O arquivo temporário `credentials.txt`, depois de usado pelo usuário para configurar o perfil protegido, foi removido do workspace e enviado à Lixeira do Windows sem que seu conteúdo fosse lido ou exibido.
- A autenticação contra um bucket ainda não foi testada porque não existe network volume. Fazer o primeiro teste somente depois de escolher o data center e criar o volume com autorização explícita.
- O catálogo Runpod ao vivo confirmou RTX 5090 32 GB a US$ 0,99/h no Secure Cloud e RTX PRO 6000 Blackwell Server Edition 96 GB a US$ 2,09/h no Secure Cloud. As duas suportam CUDA 13.0.
- `EU-RO-1` foi selecionado como data center recomendado: oferece network volume `STANDARD` e apareceu na disponibilidade das duas GPUs escolhidas. Disponibilidade é dinâmica e deve ser revalidada no provisionamento.
- A imagem pública `runpod/comfyui:cuda13.0` foi inspecionada e seu índice estava no digest `sha256:094dc6d79448b6f118c4d2b054073f92d765c568598e7a96aaeda678a6bcbf3b`.
- A recomendação atual fixa ComfyUI `v0.34.0` e `Lightricks/ComfyUI-LTXVideo` no commit `15d09abb5a18`, partindo da imagem CUDA 13 por digest.
- O modelo oficial atual verificado é LTX-2.5. O pacote de prévia INT8 completo ocupa aproximadamente 44,91 GB; adicionar o conjunto final distilled BF16 leva o inventário de modelos a aproximadamente 113,79 GB antes de software, caches e outputs.
- A arquitetura detalhada foi registrada em `docs/arquitetura-inicial.md`; a configuração legível por máquina está em `config/stack.json` e os pesos com tamanhos e SHA-256 estão em `config/models-manifest.json`.
- O `Dockerfile` derivado da imagem oficial foi preparado sem incluir pesos ou segredos. Ele substitui o bundle por ComfyUI `v0.34.0`, fixa o node LTX e preserva o contrato de inicialização oficial.
- Como o disco C: tinha cerca de 65 GB livres e o cache do Docker já ocupava cerca de 34,65 GB, o build completo local não foi iniciado. `.github/workflows/build-image.yml` prepara um build manual no GitHub Actions para GHCR, com actions fixadas por commit, cache remoto, SBOM, proveniência e digest final.
- `scripts/pod/download_models.py` baixa apenas o perfil escolhido, exige `HF_TOKEN` quando faltarem pesos e valida tamanho e SHA-256. `scripts/pod/finalize_output.py` executa FFprobe, SHA-256 e publicação atômica em `ready`.
- O repositório público é `https://github.com/BoaVistaBVShop/axi-ltx-video`. O primeiro build `v0.1.0` concluiu em 2026-08-31 com digest `sha256:d4c9faacc05976dafd64a9cd95ae133e36ec3744e79c5d653a412b18d1ae1ad4`.
- Os parâmetros iniciais estão em `config/generation-profiles.json`, documentados em `docs/parametros-ltx.md`: preview single-stage INT8 e finais two-stage INT8/BF16, 24 fps, 5 s, 8 passos na primeira etapa, CFG 1 e 3 passos de refine na segunda etapa.

Não incluir no repositório e não reproduzir em respostas o e-mail, ID interno da conta ou qualquer segredo retornado pelas ferramentas.

## 11. Estado dos artefatos e pendências atuais

Ainda não foram criados:

- imagem Docker específica do projeto;
- registry/repositório da imagem definido;
- template Runpod do projeto;
- network volume;
- Pod de configuração ou render;
- automação de subida, readiness, execução, download e teardown;
- manifest definitivo de modelos;
- benchmark de GPU;
- workload piloto.

Ainda precisa ser definido ou confirmado:

- aceitação da licença e acesso aos pesos na Hugging Face;
- repositório GitHub e decisão de visibilidade pública/privada para a imagem GHCR;
- autorização para o network volume recomendado de 200 GB `STANDARD` em `EU-RO-1`;
- benchmark real da RTX 5090 para prévia INT8 e da RTX PRO 6000 para final INT8/BF16;
- classificação de confidencialidade do material;
- duração, proporção, resolução, FPS, áudio e formato final;
- duas cenas ou referências representativas para o piloto;
- limite financeiro do piloto, proposto anteriormente em US$ 5, mas ainda não confirmado;
- autorização explícita para criar os primeiros recursos cobrados.

## 12. Serverless: decisão adiada

Não iniciar o projeto em Serverless. Primeiro estabilizar e medir o workflow em Pods. Depois que imagem, modelos, payload, tempos e qualidade estiverem consistentes, reavaliar Serverless com escala a zero.

A conversa anterior observou um worker comunitário de LTX recente e configurado para B200; ele foi considerado mais caro e arriscado para o piloto. Essa observação pode ficar desatualizada e deve ser revalidada antes de qualquer decisão futura.

## 13. Disciplina operacional para agentes futuros

- Ler este arquivo integralmente antes de agir no workspace.
- **Não usar nenhuma skill ou fluxo Forja neste projeto.** Alterações em código, configuração, documentação ou infraestrutura devem seguir diretamente as regras deste arquivo e as instruções explícitas do usuário.
- Não repetir configuração ou autenticação já concluída sem primeiro executar checagens somente de leitura.
- Usar documentação e ferramentas atuais como fonte de verdade; skills e preços registrados aqui são snapshots.
- Antes de comandos desconhecidos, consultar `runpodctl <recurso> <ação> --help` na versão instalada.
- Preferir MCP para operações simples e estruturadas de infraestrutura quando conectado.
- Usar `runpodctl` para SSH, transferência de arquivos, Hub, diagnóstico e operações que o MCP não expõe.
- Em toda criação, configurar portas, ambiente, volume, data center e proteção de tempo desde o início.
- Confirmar `runtimeStatus`/readiness e realizar uma requisição real; não tratar apenas `RUNNING` como prova de funcionamento.
- Em falha de provisionamento ou timeout, verificar se algum recurso foi criado e eliminá-lo com segurança para evitar cobrança órfã.
- Nunca excluir volume persistente, modelos ou outputs importantes sem resolver exatamente o alvo e obter autorização apropriada.
- Preservar alterações do usuário e não executar comandos destrutivos amplos.
- Manter o usuário informado antes de qualquer etapa cobrada ou decisão que altere qualidade, privacidade ou custo.
- Toda alteração futura de imagem, template, workflow ou modelo deve gerar uma nova versão registrada, com possibilidade de rollback.

## 14. Definição de pronto

A configuração inicial só estará concluída quando:

1. imagem, template, volume e scripts estiverem versionados;
2. um Pod novo puder ser criado sem instalação manual;
3. os modelos forem encontrados sem novo download completo;
4. o runtime responder ao health check;
5. uma cena de teste for gerada e validada fora do Pod;
6. o Pod for excluído automaticamente;
7. um segundo Pod novo repetir o teste usando os mesmos artefatos persistentes;
8. custo, duração, GPU e versão de todos os componentes forem registrados;
9. nenhum segredo estiver presente no repositório;
10. o usuário receber um comando ou ação simples equivalente a “iniciar, gerar e encerrar”.
