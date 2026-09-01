# Orquestração local por SSH

`scripts/local/axi-ltx.ps1` é o ponto de entrada local do fluxo operacional. Ele
controla o plano da Runpod com `runpodctl`, mas toda ação dentro de um Pod ocorre
por OpenSSH, com comandos fechados e versionados. O ComfyUI permanece acessível
somente em `127.0.0.1` por port forwarding.

## Garantias

- `known_hosts` separado por Pod em `.runpod/known_hosts/`; a verificação nunca é
  desabilitada globalmente;
- `BatchMode`, `IdentitiesOnly` e keepalive explícitos em toda conexão;
- nenhum comando de shell arbitrário é aceito pela CLI;
- nomes de job, perfil, arquivo e heartbeat são validados antes de entrar em um
  comando remoto;
- logs JSONL em `.runpod/audit/`, sem payload criativo nem variáveis secretas;
- submissão idempotente: o mesmo `job-id` retoma o `prompt_id` existente e não
  compra uma segunda geração por engano;
- bootstrap protegido por lock e marcador atômico escrito somente depois de
  downloads, checksums e health check passarem;
- teardown exige repetir exatamente o ID do Pod;
- watchdog sempre tenta excluir o Pod no deadline, no timeout de inatividade ou
  quando seu processo é interrompido.

`.runpod/` é estado operacional local e não entra no Git.

## Diagnóstico local

```powershell
.\scripts\local\axi-ltx.ps1 doctor
```

Esse comando confirma `runpodctl`, autenticação da conta, OpenSSH, `ssh-keyscan`,
imagem fixada, visibilidade pública do GHCR e a política `SSH_ONLY`. Ele não cria
recursos.

## Comandos operacionais

Os exemplos abaixo só devem ser usados depois que volume, template, Pod, custo e
deadline tiverem sido aprovados.

```powershell
# Provar SSH real e a API interna do ComfyUI por túnel efêmero
.\scripts\local\axi-ltx.ps1 readiness --pod-id POD_ID

# Preparar uma vez o perfil no network volume
.\scripts\local\axi-ltx.ps1 bootstrap --pod-id POD_ID --profile preview

# Manter um túnel local; nenhuma porta HTTP é publicada na Runpod
.\scripts\local\axi-ltx.ps1 tunnel --pod-id POD_ID --local-port 18188

# Submeter ou retomar o mesmo prompt API do ComfyUI
.\scripts\local\axi-ltx.ps1 submit --pod-id POD_ID --job-id JOB_ID `
  --prompt-json .\jobs\JOB_ID\prompt-api.json

# Validar por FFprobe/SHA-256 e mover atomicamente output-staging → ready
.\scripts\local\axi-ltx.ps1 finalize --pod-id POD_ID --job-id JOB_ID `
  --filename video.mp4

# Deadline externo; heartbeat opcional deve ficar abaixo de /workspace
.\scripts\local\axi-ltx.ps1 watchdog --pod-id POD_ID `
  --deadline 2026-09-01T23:00:00-03:00 `
  --idle-minutes 10 --heartbeat-path /workspace/jobs/JOB_ID/.activity

# Encerramento explícito e limitado a um único Pod
.\scripts\local\axi-ltx.ps1 teardown --pod-id POD_ID `
  --confirm-pod-id POD_ID --reason job-complete
```

O watchdog deve rodar em processo/terminal independente do processo de geração.
Quando o controlador de criação for adicionado, ele deverá iniciar o watchdog
antes da chamada de criação, persistir o ID retornado imediatamente e repassá-lo
ao guardião. Até esse controlador existir e ser testado, não provisionar o
primeiro Pod.

## Bootstrap e segredos

O bootstrap remoto é `/opt/ltx-stack/bootstrap.py`. Ele recebe apenas o nome do
perfil. Se `HF_TOKEN` não estiver no shell SSH, o script lê somente essa variável
do ambiente protegido do PID 1, sem imprimir seu valor, e a entrega apenas ao
processo de download. O token não vai para o network volume nem para os logs.

O estado de sucesso é gravado em:

```text
/workspace/.axi-ltx/bootstrap/<perfil>.json
```

Falha em licença, download, tamanho, SHA-256, workflow ou health check impede a
criação desse marcador.

## Recuperação segura

- timeout local de `submit` não reenvia o prompt: repita o mesmo comando e
  `job-id` para consultar o `prompt_id` já registrado;
- mudança de host/porta para o mesmo Pod é recusada. Verifique o novo endpoint
  antes de remover manualmente somente o registro daquele Pod em
  `.runpod/known_hosts/`;
- a CLI nunca exclui network volume;
- depois de `ready`, exclua o Pod antes do download S3 para interromper a GPU.
