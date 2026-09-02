# AGENTS.md — Operação de vídeo com Runpod e LTX

## 0. Fonte de verdade e precedência

Este arquivo consolida as decisões permanentes e o estado conhecido do projeto. Todo agente deve lê-lo integralmente antes de agir. Para valores exatos de máquina, `config/stack.json`, `config/models-manifest.json` e `config/generation-profiles.json` complementam este documento e devem permanecer coerentes com ele.

Em caso de conflito, aplicar esta ordem: instrução explícita mais recente do usuário → regras de segurança e autorização deste arquivo → configurações versionadas → documentação histórica. Preços, disponibilidade, versões externas e estado da conta são snapshots e devem ser revalidados por consulta somente de leitura. Uma divergência encontrada deve ser corrigida nos arquivos afetados antes de provisionar recursos; não escolher silenciosamente uma das versões.

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

### 2.1. SSH é o canal operacional obrigatório

O plano de controle da Runpod (`runpodctl`, MCP ou API) pode e deve ser usado para criar, consultar e excluir o Pod, porque não existe conexão SSH antes do provisionamento. **Depois que o Pod estiver acessível, toda configuração, bootstrap, verificação, geração, diagnóstico e manutenção dentro dele deve ser executada via SSH a partir do ambiente local controlado.**

Regras obrigatórias:

1. não usar Web Terminal, Jupyter, shell do navegador, ComfyUI Manager ou cliques na interface do Pod para instalar, configurar ou corrigir o ambiente;
2. executar por SSH somente scripts e comandos reproduzíveis, preferencialmente versionados neste repositório;
3. o bootstrap remoto deve ser idempotente: detectar o que já existe, validar versão/hash e alterar apenas o que estiver ausente ou incorreto;
4. a primeira configuração via SSH pode preparar o network volume, baixar e validar modelos, criar diretórios persistentes, executar health checks e realizar o smoke test;
5. nos Pods seguintes, o SSH deve apenas validar os artefatos persistentes, iniciar/confirmar os serviços, enviar o manifest do job, acompanhar a geração, validar/publicar os resultados e acionar o encerramento;
6. nenhuma correção feita interativamente dentro de um Pod pode ser considerada solução. A correção deve voltar para a imagem, template, script ou manifest versionado e ser testada novamente em um Pod novo;
7. se o SSH não estiver disponível, diagnosticar a porta, chave, template, readiness ou provisionamento. Não contornar o problema configurando manualmente pela interface web;
8. registrar comandos, códigos de saída e logs técnicos necessários para auditoria, sempre removendo tokens, chaves e outros segredos;
9. obter o destino SSH a partir do plano de controle da Runpod e manter verificação de host em arquivo isolado para Pods efêmeros; não desativar globalmente a verificação de host;
10. cada rotina remota deve falhar de forma explícita e segura. Uma falha não pode deixar GPU ociosa nem marcar o ambiente como configurado sem completar as validações.

O Pod deve ser criado com `22/tcp` e SSH habilitado. Na versão verificada do `runpodctl`, usar `pod create --wait --wait-timeout <limite>` para aguardar o banner SSH; isso prova apenas que o canal abriu, não que o runtime LTX está pronto. Se o wait falhar ou for interrompido, o Pod pode continuar existindo e cobrando: capturar o ID retornado, inspecionar o estado e excluir explicitamente quando não houver recuperação em andamento.

ComfyUI não será exposto por proxy HTTP no fluxo padrão. O health check e as chamadas à API devem passar por port forwarding local no SSH para a porta interna 8188. Variáveis configuradas na criação podem existir apenas no processo PID 1 e não no shell SSH; os scripts remotos devem carregar somente as variáveis necessárias por mecanismo protegido, sem `set -x`, sem ecoar valores e sem copiar segredos para o volume persistente.

O contrato operacional é, portanto: **provisionar pelo plano de controle → operar exclusivamente por SSH → publicar outputs validados em `ready` → excluir o Pod pelo plano de controle → receber pelo S3 → validar localmente**. A API S3 continua sendo o canal principal para retirar vídeos prontos; SSH/SCP não substituem o recebimento automatizado, salvo contingência documentada. A exclusão do Pod antes do download local é segura porque `ready` fica no network volume persistente e reduz o tempo cobrado de GPU.

## 3. Por que não manter um único Pod parado

- O container disk é efêmero e seu conteúdo é apagado ao parar/reiniciar ou resetar o Pod.
- O volume disk local preserva `/workspace` durante stop/start, mas é apagado quando o Pod é terminado e continua gerando cobrança enquanto o Pod está parado.
- A cobrança documentada para volume disk parado é maior que a do mesmo volume durante execução. Verificar sempre a tarifa atual antes de tomar decisões.
- Parar libera a GPU; reiniciar depois não garante que a mesma GPU, ou qualquer GPU, estará disponível.
- Pods anexados a network volumes atualmente não podem ser parados; precisam ser terminados. O network volume sobrevive independentemente.
- Portanto, o fluxo padrão deste projeto é **create → render → validate/publish to `ready` → delete Pod → download/backup via S3**, e não stop/start.

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
9. conectar ao Pod de configuração por SSH e executar o bootstrap idempotente versionado;
10. carregar e validar os modelos no volume apenas uma vez por meio desse bootstrap SSH;
11. executar por SSH um smoke test real de ponta a ponta;
12. baixar e validar o artefato de teste;
13. terminar o Pod de configuração/teste e confirmar que imagem, template e volume bastam para recriá-lo.

O teste de aceitação da configuração inicial é: criar um Pod do zero usando somente os artefatos persistentes, gerar uma pequena cena válida, obter o resultado fora do Pod e excluir o Pod sem perder modelos, workflows ou configuração.

## 5. Fluxo normal de cada workload

1. **Antes de pedir que o usuário envie a cena, imagens, vídeos ou folha de referências, perguntar explicitamente se aquela cena terá teste A/B sem LoRA versus com LoRA e, em caso afirmativo, qual perfil de LoRA será avaliado.** Não presumir a resposta a partir de cenas anteriores. Se o material chegar antes da pergunta, perguntar imediatamente, antes de fechar o manifest, estimar custo ou ligar GPU.
2. Receber cenas, imagens ou vídeos de referência e o briefing.
3. Registrar duração, proporção, resolução, FPS, requisitos de áudio, estilo, continuidade, privacidade e formato de entrega.
4. Registrar no manifest a decisão `lora_ab`: habilitado ou não, perfil solicitado, baseline, inputs de controle e critérios de comparação.
5. Montar um manifest de job reproduzível.
6. Estimar GPU, tempo, armazenamento temporário e custo máximo.
7. Obter aprovação explícita antes de criar qualquer recurso cobrado quando ainda não houver autorização para aquele lote.
8. Criar o Pod pelo template, no mesmo data center do network volume.
9. Armar antes da criação um deadline externo/watchdog que exclua o Pod; usar também proteção nativa de expiração somente se a capacidade existir e for confirmada no plano de controle ao vivo.
10. Aguardar a disponibilidade do SSH e conectar usando o destino fornecido pelo plano de controle.
11. Executar por SSH a validação idempotente do ambiente e verificar ComfyUI/runtime LTX com uma chamada de saúde, não apenas pelo status `RUNNING`.
12. Enviar ou referenciar o workload e iniciar a geração por SSH.
13. Gerar, por padrão, duas variações A/B por cena na mesma inicialização e com seeds registradas. O A/B criativo entre seeds é distinto do A/B técnico sem LoRA versus com LoRA; este último só ocorre após a confirmação específica do usuário prevista no passo 1.
14. Apresentar as prévias e renderizar em qualidade final somente as versões aprovadas.
15. Fazer upscale e pós-processamento apenas quando agregarem qualidade à versão escolhida.
16. Executar controle de qualidade técnico e visual remoto.
17. Publicar atomicamente resultados, manifests, checksums e metadados validados em `ready` no network volume.
18. Terminar/excluir o Pod assim que a fila acabar e `ready` estiver seguro; não esperar o download local com a GPU ligada.
19. Baixar pela API S3 para `.incoming` e validar novamente SHA-256 e FFprobe localmente.
20. Mover o job validado para `entregas/<job-id>` e confirmar backup antes de limpar o remoto.
21. Remover intermediários descartáveis sem apagar pesos ou workflows persistentes nem entregas não confirmadas.
22. Registrar tempo, custo, GPU, modelo, workflow, seed, LoRA/força, inputs de controle e resultado para melhorar estimativas futuras.

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

### 5.2. Contrato do job e idempotência

Cada workload deve ter um `job-id` único e um manifest versionado. No mínimo, registrar: briefing e referências; classificação de confidencialidade; prompt e negative prompt; seeds A/B e seed escolhida; perfil, modelo, hashes e workflow; duração, frames, FPS, proporção e resoluções; áudio; GPU e data center; limites de tempo e custo; timestamps; estados; outputs esperados; checksums; resultados de QC; custo total, por cena e por segundo aprovado.

Reexecutar um comando SSH para o mesmo `job-id` não pode sobrescrever silenciosamente um output pronto nem criar cobrança duplicada. A automação deve detectar o estado persistido, retomar apenas etapas incompletas quando isso for seguro e exigir um novo identificador ou autorização explícita para substituir resultados existentes.

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

### 6.1. Perfis iniciais já definidos

Os valores executáveis estão em `config/generation-profiles.json` e não devem ser alterados sem versionar a decisão:

- padrão: 24 fps, 5 segundos e 121 frames pela regra `1 + 8k`;
- prévia: workflow single-stage distilled INT8, RTX 5090 32 GB, 8 passos, CFG 1 e `euler_ancestral`;
- finais iniciais: workflow two-stage na RTX PRO 6000 96 GB, primeira etapa com 8 passos e segunda com upscale espacial 2×, refine de 3 passos, CFG 1 e reutilização da seed aprovada;
- `final-int8` é o perfil final inicial; `final-bf16` só pode virar padrão depois de benchmark real comprovar ganho de qualidade/custo/tempo que justifique o peso adicional;
- formatos de entrega: 16:9 em 1920×1080, 9:16 em 1080×1920 e 1:1 em 1080×1080, usando o crop versionado correspondente;
- duas seeds explícitas nas prévias e reutilização da seed selecionada no final;
- áudio habilitado por padrão, sujeito ao briefing do job;
- prompt enhancement desligado inicialmente para preservar reprodutibilidade; se habilitado, armazenar texto expandido e seed.

### 6.2. LoRAs e protocolo A/B obrigatório

As LoRAs são capacidades opcionais pré-configuradas, não melhoradores automáticos de qualidade. Elas permanecem no network volume depois do primeiro download validado, mas somente o perfil escolhido pelo usuário pode ser carregado no job. A ausência de resposta afirmativa significa `lora_ab.enabled=false` e execução baseline; nunca ativar uma LoRA silenciosamente.

Antes de o usuário enviar cada cena, perguntar se haverá comparação A/B sem LoRA versus com LoRA. A pergunta deve ocorrer enquanto nenhuma GPU estiver ativa. Se a resposta for afirmativa, confirmar qual destes controles será testado e pedir o input correspondente:

- `lora_ingredients_bf16`: folha de referência com personagens, produto/objetos, figurino e cenário;
- `lora_motion_track_bf16`: imagem/vídeo de referência e trajetórias de movimento;
- `lora_union_control_bf16`: guia estrutural compatível com o workflow Union Control;
- `lora_outpaint_bf16`: vídeo de origem e máscara binária para inpainting/outpainting.

O teste deve comparar o perfil LoRA com o baseline BF16 indicado em `config/generation-profiles.json`, mantendo travados prompt, negative prompt, seed, materiais de entrada, duração, FPS, frames, aspecto e resolução. Registrar as diferenças de workflow e controle no manifest. Não comparar INT8 baseline com BF16+LoRA como se a diferença viesse apenas da LoRA.

Os workflows oficiais LTX-2.5 fixados neste projeto carregam transformer e text encoder distilled BF16 e adaptadores IC-LoRA treinados sobre LTX-2.3. O piloto de 2026-09-01 comprovou baseline BF16 e os quatro pesos LoRA na RTX PRO 6000 96 GB. Ingredients, Motion Track e Outpaint completaram smoke tests reais; Union Control completou somente com guia RGB direto e sem a segunda etapa, porque os annotators de depth do workflow oficial não fazem parte da imagem vigente. Portanto, Union Control ainda não é perfil de produção. INT8+LoRA, RTX 5090 e outras combinações só podem ser habilitadas depois de teste real demonstrar compatibilidade, qualidade e custo.

Para o lote Axi360 recebido em 2026-09-01, a documentação oficial atual foi revalidada e afirma explicitamente que o `ICLoraPipeline` só pode ser usado com modelo distilled. Portanto, o A/B executável e tecnicamente válido deste lote é **LTX-2.5 distilled BF16 baseline versus o mesmo LTX-2.5 distilled BF16 com Ingredients 1.3**. Não apresentar esse teste como Dev/Pro versus LoRA e não combinar Dev BF16 com Ingredients como se fosse suportado oficialmente. O A/B responde se o workflow Ingredients com referência melhora a preservação e o controle, mantendo transformer, text encoder, Diffusion Video VAE, seed, FPS, duração, resolução-base, prompt e demais parâmetros comparáveis travados. Os perfis versionados são `axi360_ingredients_ab_baseline` e `axi360_ingredients_ab_variant`.

O commit fixado `15d09abb5a18` do `ComfyUI-LTXVideo` é incompatível com Kornia 0.8.3 sem o reparo do upstream que remove o import de `pad` e usa `torch.nn.functional.pad`. O reparo idempotente está versionado em `scripts/pod/repair_ltx_kornia.py`, incorporado ao `Dockerfile` e publicado na imagem `v0.4.0`. A imagem `v0.3.0` permanece apenas como histórico e não deve ser usada em novas sessões.

Pesos e workflows são baixados idempotentemente uma única vez para o volume persistente e validados por tamanho e SHA-256 a partir de `config/models-manifest.json` e `config/workflows-manifest.json`. Uma falha de licença, download ou checksum deve interromper o bootstrap sem marcar o perfil como pronto.

## 7. GPUs e qualidade — decisão atual e hipóteses históricas

Decisão inicial vigente para o piloto:

- prévia: LTX distilled INT8 em RTX 5090 32 GB;
- final padrão inicial: LTX distilled INT8 two-stage em RTX PRO 6000 96 GB;
- BF16 na RTX PRO 6000 só depois de benchmark comparativo;
- A100 80 GB é fallback de disponibilidade/compatibilidade, não padrão;
- modelo full/SFT em H200 141 GB é excepcional e exige evidência de ganho e autorização específica de custo.

Preços observados anteriormente, apenas como registro histórico e não como cotação válida:

- RTX 5090: US$ 0,69/h Community ou US$ 0,99/h Secure;
- A100 SXM 80 GB: US$ 1,39/h Community ou US$ 1,59/h Secure;
- RTX PRO 6000 96 GB: US$ 1,69/h Community ou US$ 2,09/h Secure;
- H200 141 GB: US$ 3,59/h Community ou US$ 4,59/h Secure.

Esses valores e a própria disponibilidade podem mudar. Consultar o catálogo ao vivo da conta antes de cada provisionamento. Não escolher GPU somente por nome ou preço: confirmar VRAM, compatibilidade, velocidade medida, disponibilidade no data center do volume e custo por segundo aprovado de vídeo.

**Secure Cloud é a arquitetura aprovada e o padrão deste projeto**, inclusive por causa do network volume e do fluxo S3. Community Cloud não deve ser escolhida automaticamente; só pode ser avaliada para material não confidencial mediante pedido ou autorização explícita do usuário, confirmação de compatibilidade do armazenamento e nova análise de risco/custo.

## 8. Regras obrigatórias de custo

- Nenhum Pod, volume, endpoint ou outro recurso cobrado deve ser criado sem autorização explícita do usuário quando a autorização do lote ainda estiver pendente.
- Antes da primeira cobrança, apresentar configuração, custo horário, estimativa do lote, custo persistente e limite máximo.
- Usar um deadline rígido por workload com exclusão automática do Pod. A ajuda ao vivo do `runpodctl` 2.12.0 não expõe `--terminate-after`; portanto, não depender dessa flag. Implementar watchdog externo que chame a exclusão pelo plano de controle e usar uma expiração nativa apenas quando a ferramenta/API em uso confirmar suporte.
- Manter um watchdog de inatividade; a hipótese anterior foi terminar após aproximadamente 10 minutos sem trabalho, mas o encerramento imediato ao fim da fila é preferível quando todos os outputs já estiverem seguros.
- Terminar/excluir o Pod, não apenas pará-lo, no fluxo padrão.
- Não manter GPU ociosa enquanto se espera briefing, escolha criativa ou envio de arquivos.
- Gerar A/B na mesma carga do modelo quando isso reduzir retrabalho.
- Fazer upscale e render final somente nas versões selecionadas.
- Limpar outputs temporários e caches que possam ser recriados, sem apagar pesos ou workflows persistentes.
- Registrar custo total, custo por cena e custo por segundo aprovado.
- O spend limit geral da conta não substitui um limite específico do workload.
- O network volume gera cobrança enquanto existir, mesmo sem Pod. Revisar periodicamente ocupação e necessidade.

O inventário técnico de 2026-08-31 levou à recomendação de um network volume `STANDARD` de 200 GB, estimado em cerca de US$ 14/mês na tarifa de referência de US$ 0,07/GB/mês. O usuário autorizou explicitamente esse custo e o volume foi criado e verificado em 2026-09-01. A cobrança persistente fica ativa enquanto o volume existir. A recomendação, os cálculos e o estado provisionado estão em `docs/arquitetura-inicial.md`.

O piloto sugerido anteriormente tinha limite de US$ 5, duas cenas, duas prévias por cena e uma final selecionada de cada. Esse limite ainda precisa de confirmação explícita antes de provisionar recursos.

## 9. Segurança, credenciais e privacidade

- Nunca pedir ao usuário que envie API keys, tokens ou senhas pelo chat.
- Nunca imprimir credenciais em logs, respostas, commits, manifests ou arquivos deste workspace.
- Nunca gravar credenciais dentro da imagem Docker ou do `AGENTS.md`.
- Usar variáveis de ambiente, secrets do Runpod, arquivos de configuração fora do repositório ou mecanismos do registry.
- A `RUNPOD_API_KEY` já foi configurada localmente pelo usuário por meio de `runpodctl doctor`; não pedir a chave novamente sem evidência de que expirou ou foi revogada.
- O usuário aceitou o acesso ao LTX-2.5 e aos repositórios gated Ingredients e In/Outpainting, e criou um token Hugging Face de leitura; o valor foi cadastrado diretamente no secret Runpod `hf_token` e nunca foi enviado pelo chat. O template deve expô-lo somente como `HF_TOKEN={{ RUNPOD_SECRET_hf_token }}`. Não pedir ou reproduzir o valor. Os aceites compartilharam com a Lightricks o e-mail e nome de usuário da conta, com confirmação explícita do usuário imediatamente antes da submissão.
- Chaves S3 da Runpod são distintas da API key de infraestrutura. Só criar/configurar S3 se o fluxo realmente usar a API compatível com S3.
- O usuário informou que as credenciais S3 da Runpod já foram criadas no Console. Não criar outra chave sem necessidade. A configuração local ainda deverá ser feita diretamente pelo usuário, sem enviar o segredo pelo chat.
- Não reutilizar a API key Runpod como credencial de Hugging Face, Docker, GitHub, S3 ou qualquer outro serviço.
- Não incluir dados pessoais da conta em documentação ou relatórios desnecessários.
- Material de cliente confidencial não deve ser colocado em Community Cloud sem autorização consciente do usuário.
- Backups importantes devem existir fora da Runpod; a Runpod não deve ser tratada como armazenamento permanente de longo prazo.
- O acesso SSH deve usar chave pública cadastrada na Runpod; nunca armazenar chave privada no repositório, imagem, volume ou chat.
- ComfyUI e outros serviços internos devem ser acessados por SSH/port forwarding no fluxo padrão. Não expor portas web desnecessárias nem usar uma interface exposta para configuração.

## 10. Estado técnico verificado até 2026-09-01

- Sistema local: Windows com PowerShell.
- Workspace: `C:\Users\Guilherme\dev\freelancers\2025\acao_conecta\bvshop\edição videos big one`.
- Plugin oficial Runpod `runpod@runpod` versão 1.2.0 foi instalado/habilitado na configuração anterior.
- O conjunto de oito skills Runpod estava instalado: router principal e lanes auxiliares, incluindo `runpod-migrate` e `runpod-templates`.
- MCP hospedado configurado em `https://mcp.getrunpod.io/` e autenticado via OAuth.
- O OAuth do MCP autentica apenas o MCP; ele não substitui a API key necessária para operações CLI como transferência de arquivos, SSH, Hub e automações locais.
- `runpodctl` 2.12.0 foi instalado em `C:\Users\Guilherme\AppData\Local\Programs\runpodctl\runpodctl.exe`.
- O SHA-256 do binário foi validado contra o checksum publicado na release oficial e o diretório foi adicionado ao PATH do usuário.
- No shell atual, o PATH novo ainda não foi herdado; até uma nova sessão confirmá-lo, usar o caminho absoluto acima nos scripts locais.
- A API key foi configurada pelo usuário diretamente no terminal, sem passar pelo chat.
- `runpodctl user` respondeu com sucesso.
- Snapshot observado da conta nessa data: saldo de US$ 10, gasto corrente de US$ 0/h e spend limit geral de US$ 80/h. Esses números são temporários e devem ser consultados novamente antes de qualquer custo.
- Ao fim do piloto, `runpodctl pod list --all` retornou lista vazia; não restou GPU ou Pod cobrando.
- `runpodctl ssh list-keys` confirmou uma chave pública cadastrada na conta. A chave privada correspondente permanece local e nunca deve ser lida, copiada ou registrada no projeto.
- A ajuda ao vivo de `runpodctl pod create` confirmou `--ssh`, `--ports`, `--wait` e `--wait-timeout`, mas não listou `--terminate-after`; o watchdog externo é obrigatório para o primeiro provisionamento.
- `flash` não foi instalado porque ainda não é necessário para o fluxo inicial baseado em Pods. Só instalar se uma futura etapa serverless/code-first justificar.
- O usuário informou que já criou credenciais S3 no Console Runpod.
- Docker Desktop com Docker Engine 29.7.2 e backend Linux/amd64 foi validado como operacional após a atualização do WSL. A versão do Docker Desktop observada antes dessa atualização foi 4.84.0; consultar novamente quando a versão exata importar.
- O WSL foi atualizado de 2.1.5 para 2.7.12 para corrigir `WSL/Service/E_UNEXPECTED` no primeiro boot de uma distribuição normal.
- Ubuntu 26.04.1 LTS foi instalado no WSL2 e inicializado como ambiente dedicado de automação.
- AWS CLI 2.36.35 foi instalado dentro do Ubuntu/WSL e validado com `aws --version`.
- O perfil local `runpod-s3` foi configurado pelo usuário diretamente no prompt interativo. A validação confirmou apenas os prefixos esperados (`user_...` e `rps_...`) sem exibir valores; `/root/.aws/credentials` e `/root/.aws/config` ficaram com permissão `600`.
- `scripts/verify_runpod_s3_profile.sh` verifica com segurança a presença, os formatos e as permissões do perfil sem imprimir as credenciais.
- O arquivo temporário `credentials.txt`, depois de usado pelo usuário para configurar o perfil protegido, foi removido do workspace e enviado à Lixeira do Windows sem que seu conteúdo fosse lido ou exibido.
- O network volume `axi-ltx-video-models-v1` (`134w7utxe6`) foi criado com autorização explícita e verificado como `STANDARD`, 200 GB, em `EU-RO-1`. A listagem pela API S3 regional com o perfil local protegido `runpod-s3` foi bem-sucedida; o volume estava vazio e nenhum segredo foi exibido.
- O template `axi-ltx-video-v0.4.0` (`6213ek6yok`) foi verificado com a imagem pública pelo digest imutável vigente, container disk de 150 GB, montagem em `/workspace`, somente `22/tcp`, sem autenticação de registry e com referência ao secret existente pela variável `HF_TOKEN`. Nenhuma porta HTTP ou de Jupyter foi publicada.
- O piloto autorizado usou Pods descartáveis com watchdog, primeiro em RTX 5090 e depois em RTX PRO 6000 96 GB Secure. O Pod final `w3l8tr21pq6zpl` foi criado às 21:03:35, excluído às 21:34:57 e teve custo estimado de US$ 1,092 a US$ 2,09/h, abaixo do corte de 21:50 e do teto autorizado.
- O catálogo Runpod ao vivo confirmou RTX 5090 32 GB a US$ 0,99/h no Secure Cloud e RTX PRO 6000 Blackwell Server Edition 96 GB a US$ 2,09/h no Secure Cloud. As duas suportam CUDA 13.0.
- `EU-RO-1` foi selecionado como data center recomendado: oferece network volume `STANDARD` e apareceu na disponibilidade das duas GPUs escolhidas. Disponibilidade é dinâmica e deve ser revalidada no provisionamento.
- A imagem pública `runpod/comfyui:cuda13.0` foi inspecionada e seu índice estava no digest `sha256:094dc6d79448b6f118c4d2b054073f92d765c568598e7a96aaeda678a6bcbf3b`.
- A recomendação atual fixa ComfyUI `v0.34.0` e `Lightricks/ComfyUI-LTXVideo` no commit `15d09abb5a18`, partindo da imagem CUDA 13 por digest.
- O modelo oficial atual verificado é LTX-2.5. O pacote de prévia INT8 completo ocupa aproximadamente 44,91 GB; adicionar o conjunto final distilled BF16 leva o inventário-base a aproximadamente 113,79 GB. Os quatro IC-LoRAs configurados adicionam aproximadamente 3,60 GB, totalizando cerca de 117,39 GB antes de software, caches e outputs.
- A arquitetura detalhada foi registrada em `docs/arquitetura-inicial.md`; a configuração legível por máquina está em `config/stack.json` e os pesos com tamanhos e SHA-256 estão em `config/models-manifest.json`.
- O `Dockerfile` derivado da imagem oficial foi preparado sem incluir pesos ou segredos. Ele substitui o bundle por ComfyUI `v0.34.0`, fixa o node LTX e preserva o contrato de inicialização oficial.
- Como o disco C: tinha cerca de 65 GB livres e o cache do Docker já ocupava cerca de 34,65 GB, o build completo local não foi iniciado. O build foi transferido para o GitHub Actions, que publicou a imagem com SBOM, proveniência e digest imutável.
- `scripts/pod/download_models.py` baixa apenas o perfil escolhido, exige `HF_TOKEN` quando faltarem pesos e valida tamanho e SHA-256. `scripts/pod/finalize_output.py` executa FFprobe, SHA-256 e publicação atômica em `ready`.
- O repositório público é `https://github.com/BoaVistaBVShop/axi-ltx-video`. Os builds `v0.1.0`, `v0.2.0` e `v0.3.0` permanecem como histórico. A imagem vigente `v0.4.0`, construída pelo GitHub Actions a partir do commit `15bcce981f7150774bad154137c4a77fb37790c2`, concluiu com sucesso no run `33576333768` e tem digest imutável `sha256:fc9a3461e44f0152076deff2bd1fc9581f94fe5eb97950fe19c65d72216c14fd`; ela contém os manifests, perfis, downloaders, `/opt/ltx-stack/bootstrap.py` e o reparo LTX/Kornia.
- Por decisão explícita mais recente do usuário, o pacote `axi-ltx-video` no GHCR é **público**. A permissão organizacional para criação de pacotes públicos foi habilitada com autorização específica, e o pull anônimo da tag e do digest vigente foi comprovado com `docker buildx imagetools inspect`. O template Runpod não deve usar autenticação de container registry para esta imagem. Tornar a imagem pública não expõe `HF_TOKEN`, pesos gated, credenciais S3 nem materiais de cliente, pois esses artefatos não fazem parte da imagem.
- Os parâmetros iniciais estão em `config/generation-profiles.json`, documentados em `docs/parametros-ltx.md`: preview single-stage INT8 e finais two-stage INT8/BF16, 24 fps, 5 s, 8 passos na primeira etapa, CFG 1 e 3 passos de refine na segunda etapa.
- O usuário aceitou o acesso ao LTX-2.5 e, após confirmação específica sobre o compartilhamento de dados, aceitou também os repositórios gated Ingredients e In/Outpainting. Criou um token Hugging Face somente leitura e armazenou seu valor como secret Runpod `hf_token`; o conteúdo do token não foi visto nem registrado no workspace.
- Os pesos baseline BF16 e dos quatro IC-LoRAs foram baixados uma vez, validados por tamanho/SHA-256 e persistidos no network volume. Ingredients, Motion Track e Outpaint passaram por geração real; Union Control passou somente pelo caminho adaptado descrito acima e permanece pendente no workflow oficial completo.
- A orquestração local SSH em `scripts/local/runpod_ssh.py` foi validada em infraestrutura real: criação protegida, host key isolada, SSH, túnel local, bootstrap idempotente, submissão, retomada, métricas pontuais, finalize, watchdog e teardown exato. Vinte e um testes locais também passaram após o piloto.
- O fluxo S3 real foi comprovado de ponta a ponta: `ready` remoto → exclusão do Pod → `.incoming` local → SHA-256 e FFprobe → `entregas/axi360-clip-01-20260901/previews`. Nenhuma credencial foi impressa.
- A cena 1 gerou baseline INT8, baseline BF16 e quatro saídas LoRA, todas com 3,041667 s, 24 fps, H.264 e áudio AAC. Depois do recebimento, o usuário corrigiu a especificação: a entrega deveria ter 10 segundos e 60 fps. Portanto, essas seis saídas são somente previews técnicos A/B e não a entrega válida. Os tempos totais observados na RTX PRO 6000 foram 268,373 s para o primeiro baseline BF16, 50,495 s para Ingredients, 28,077 s para Motion Track, 31,801 s para Union adaptado e 37,450 s para Outpaint. Ingredients atingiu amostra pontual de 100% de GPU e 73,1% de VRAM; as demais amostras pontuais não representam pico e não devem ser apresentadas como média.
- Em 2026-09-01/02, o usuário escolheu Motion Track e autorizou o mesmo perfil para as cinco cenas do lote `axi360-motion-track-10s-20260902`. Cada cena foi gerada em BF16 com seed `31001`, 512×960, 24 fps e 241 frames/10,041667 s; depois do recebimento S3 e da dupla validação, a conversão local por estimativa de movimento entregou exatamente 600 frames, 60 fps e 10,000000 s. Os tempos de geração registrados foram 99,217 s, 43,810 s, 43,394 s, 43,244 s e 40,280 s para as cenas 1–5. A sessão RTX PRO 6000 96 GB Secure durou 813,134 s a US$ 2,09/h, custo estimado de US$ 0,4721, abaixo do teto de US$ 0,60. Amostras pontuais observaram pico de 100% da GPU, 75.783 MiB/97.887 MiB de VRAM (77,42%), 59 °C e 591,41 W; não tratar esses números como médias. O Pod foi excluído antes do download e `runpodctl pod list --all` retornou vazio. Os cinco outputs remotos permanecem em `ready` até confirmação de recebimento/backup. O QC visual marcou as cenas 3 e 4 para revisão por divergirem do estilo claro da referência; cenas 1, 2 e 5 preservaram melhor a direção visual.
- O novo lote Axi360 de cinco cenas foi recebido com cinco start frames 9:16 e prompt-fonte integral salvo fora do Git em `jobs/axi360-dev-ingredients-ab-20260901/inputs/prompts.txt`; a segunda cópia enviada pelo usuário foi confirmada como idêntica pelo SHA-256 `98e6a1ade035022a5be689820fc614ddb028b12f8455c8293fab9c7afaacf7ae`. O job pede 25 fps, áudio de saída desativado, seed única, 544×960 na geração e entrega 1080×1920, com durações geradas 6/8/6/8/6 segundos e pós-produção posterior. O usuário autorizou uma RTX PRO 6000 96 GB Secure a US$ 2,09/h com teto rígido de 50 minutos a partir da criação do Pod, equivalente a no máximo aproximadamente US$ 1,74 de GPU; o watchdog não pode ser prorrogado sem nova autorização.

Não incluir no repositório e não reproduzir em respostas o e-mail, ID interno da conta ou qualquer segredo retornado pelas ferramentas.

## 11. Estado dos artefatos e pendências atuais

Concluído e versionado:

- repositório público `https://github.com/BoaVistaBVShop/axi-ltx-video`, branch `main`;
- Dockerfile, build no GitHub Actions e imagem vigente `ghcr.io/boavistabvshop/axi-ltx-video:v0.4.0` publicada no digest imutável registrado em `config/stack.json`; versões anteriores permanecem apenas como histórico;
- manifest de modelos com tamanho e SHA-256, perfis de geração e arquitetura inicial;
- manifest de workflows fixado por commit e SHA-256, quatro perfis IC-LoRA BF16 selecionáveis e política obrigatória de confirmação A/B por cena;
- scripts no Pod para download/validação idempotente de modelos e publicação atômica de outputs em `ready`;
- script idempotente para baixar e validar workflows no network volume;
- CLI local e bootstrap remoto versionados, com `known_hosts` isolado por Pod, auditoria redigida, túnel apenas em localhost, retomada por `job-id`, watchdog e teardown limitado a um ID exato;
- controlador de criação protegida versionado: exige autorização local de uso único vinculada a custo e recursos, revalida ao vivo preço/estoque da GPU, digest do template e local/tamanho/tier do volume, persiste a intenção, arma um guardião independente antes de criar, usa nome UUID exclusivo, persiste o ID antes da espera SSH e permite ao guardião redescobrir/excluir Pods próprios se o processo principal cair;
- ambiente local Runpod autenticado, `runpodctl` funcional e perfil S3 configurado e validado contra o volume real sem segredos no repositório;
- network volume `axi-ltx-video-models-v1` (`134w7utxe6`) provisionado e verificado como `STANDARD`, 200 GB, em `EU-RO-1`;
- template `axi-ltx-video-v0.4.0` (`6213ek6yok`) provisionado e verificado com imagem fixada por digest, somente SSH em `22/tcp`, montagem em `/workspace` e secret referenciado sem expor o valor;
- lote Motion Track de cinco cenas recebido localmente com SHA-256 verificado e entrega exata de 10 s/60 fps/600 frames; manifest, checksums e QC ficam em `entregas/axi360-motion-track-10s-20260902` e não entram no Git;
- decisão de Secure Cloud, RTX 5090 para prévia e RTX PRO 6000 para final, ainda sujeita à revalidação de preço e disponibilidade antes de cada Pod.

Ainda não concluído:

- instalar e fixar os annotators exigidos pelo workflow oficial Union Control, validar seus pesos/hashes e repetir o teste com depth real e segunda etapa;
- transformar a amostragem pontual de GPU em telemetria contínua por job, com média, pico e série temporal persistida; não inferir picos para os renders já concluídos;
- o usuário revisar as cinco entregas Motion Track, principalmente as cenas 3 e 4, que divergiram do estilo claro da referência;
- confirmar editor e codec master antes de gerar ProRes 422 HQ ou DNxHR HQX;
- fazer purge dos outputs remotos somente depois de o usuário confirmar recebimento e backup;
- cada novo lote pago continua exigindo estimativa, teto e autorização explícita conforme a seção 8.

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
- MCP, API e `runpodctl` são permitidos para o plano de controle da Runpod; eles não substituem o SSH para ações dentro do Pod.
- Usar `runpodctl` para descobrir/estabelecer o acesso SSH, transferência de contingência, Hub, diagnóstico e operações que o MCP não expõe.
- Depois do provisionamento, tratar SSH como o único canal autorizado para qualquer ação dentro do Pod; interfaces web podem ser usadas apenas para observação, nunca para configuração ou correção.
- Não executar comandos remotos avulsos que não possam ser reproduzidos. Transformar a operação em script versionado e idempotente antes de considerá-la parte do fluxo oficial.
- Em toda criação, configurar portas, ambiente, volume, data center e proteção de tempo desde o início.
- Confirmar `runtimeStatus`/readiness e realizar uma requisição real; não tratar apenas `RUNNING` como prova de funcionamento.
- Em falha de provisionamento ou timeout, verificar se algum recurso foi criado e eliminá-lo com segurança para evitar cobrança órfã.
- Nunca excluir volume persistente, modelos ou outputs importantes sem resolver exatamente o alvo e obter autorização apropriada.
- Preservar alterações do usuário e não executar comandos destrutivos amplos.
- Manter o usuário informado antes de qualquer etapa cobrada ou decisão que altere qualidade, privacidade ou custo.
- Antes de pedir o envio de cada cena, perguntar sempre se haverá teste A/B sem LoRA versus com LoRA; registrar a resposta e nunca reaproveitar automaticamente a escolha de outra cena.
- Toda alteração futura de imagem, template, workflow ou modelo deve gerar uma nova versão registrada, com possibilidade de rollback.
- Não usar as imagens em `docs/assets/` como fonte normativa; elas são explicações visuais. Em divergência, prevalecem este arquivo e os manifests versionados.

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
10. toda configuração e operação interna tiver sido executada e repetida exclusivamente via SSH;
11. um segundo Pod provar que o bootstrap SSH não reinstala nem rebaixa o que já está correto;
12. o downloader S3 provar `ready → .incoming → entregas/<job-id>` com dupla validação e sem Pod ativo;
13. falhas de bootstrap, render ou download deixarem estado retomável e não deixarem Pod/GPU órfão;
14. o usuário receber um comando ou ação simples equivalente a “iniciar, gerar, receber e encerrar”, sem configuração manual recorrente.
15. cada perfil LoRA habilitado tiver passado por download autenticado, checksum e A/B real contra o baseline BF16 correspondente, mantendo os parâmetros comparáveis travados.
