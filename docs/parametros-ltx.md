# Parâmetros de geração LTX-2.5

Snapshot: 2026-08-31. Os valores de `config/generation-profiles.json` são o ponto de partida do projeto e devem ser confirmados no benchmark real.

## O que muda em cada cena

| Parâmetro | Função | Regra do projeto |
|---|---|---|
| `profile` | Decide velocidade/qualidade | `preview`, `final_int8` ou `final_bf16` |
| `aspect` | Formato do vídeo | `landscape_16_9`, `portrait_9_16` ou `square_1_1` |
| `positive_prompt` | Cena, ação, câmera, iluminação e áudio | Obrigatório e armazenado no manifest do job |
| `negative_prompt` | Restrições específicas | Vazio por padrão; só adicionar problemas concretos |
| `duration_seconds` | Duração pedida | 5 s por padrão; o número de frames obedece `1 + 8k` |
| `seed` | Reprodutibilidade | Explícita; duas sementes nas prévias A/B |
| `use_image_input` | Liga T2V ou I2V | `false` para texto; `true` para usar uma imagem inicial |
| `input_image` | Primeiro quadro/referência visual | Obrigatório quando `use_image_input=true` |
| `audio_enabled` | Gera áudio sincronizado | Ligado por padrão; pode ser desligado por job |
| `lora_ab.enabled` | Autoriza comparar baseline e LoRA | Sempre perguntar antes do envio de cada cena; `false` por padrão |
| `lora_ab.profile` | Seleciona o controle LoRA | Ingredients, Motion Track, Union Control ou Outpaint |

## O que fica travado no perfil

O modelo distilled usa uma agenda curta de oito passos e já incorpora guidance pela destilação. Por isso o CFG padrão é `1.0`; a documentação oficial recomenda, se houver experimentação, permanecer aproximadamente entre `1.0` e `1.5`.

### Preview

- workflow single-stage;
- transformer e text encoder INT8 ConvRot;
- oito passos, CFG 1, `euler_ancestral`;
- sem upscale/refine;
- tiles conservadores para a RTX 5090 de 32 GB;
- duas variações A/B por cena.

### Final INT8

- workflow two-stage;
- primeira etapa com oito passos;
- upscale espacial 2×;
- refinamento de três passos na segunda etapa;
- mesma seed escolhida na prévia;
- tiles maiores na RTX PRO 6000 de 96 GB, com fallback conservador.

### Final BF16

É igual ao final INT8, mas troca transformer e text encoder pelos pesos BF16. Só deve ser usado depois que um benchmark lado a lado provar ganho visual suficiente para justificar download, armazenamento e tempo maiores.

### Perfis IC-LoRA

O piloto de 2026-09-01 validou em GPU real o baseline BF16, Ingredients, Motion Track e Outpaint. Union Control passou somente por smoke test adaptado com guia RGB e single-stage; o caminho oficial com annotator de depth e segunda etapa continua pendente:

| Perfil | Finalidade | Input obrigatório | Baseline A/B |
|---|---|---|---|
| `lora_ingredients_bf16` | consistência de personagem, produto, figurino e cenário | folha de referências | `baseline_bf16_single_stage` |
| `lora_motion_track_bf16` | controlar trajetórias | imagem/vídeo e motion tracks | `baseline_bf16_single_stage` |
| `lora_union_control_bf16` | controle estrutural combinado | guia Union Control | `final_bf16` |
| `lora_outpaint_bf16` | preencher ou expandir vídeo | vídeo de origem e máscara binária | `final_bf16` |

Antes de pedir o envio de cada cena, perguntar se haverá teste A/B com LoRA e qual perfil será usado. A resposta não é herdada de outra cena. Sem confirmação, nenhum peso LoRA é carregado. Quando autorizado, baseline e variante mantêm iguais prompt, negative prompt, seed, inputs, duração, FPS, frames, aspecto e resolução.

Os exemplos oficiais LTX-2.5 usados aqui combinam transformer/text encoder distilled BF16 com IC-LoRAs treinados sobre LTX-2.3. Os primeiros testes, portanto, usam RTX PRO 6000 96 GB. Não atribuir diferenças entre INT8 e BF16 à LoRA e não habilitar INT8+LoRA até existir validação específica.

## Resolução e entrega

O workflow two-stage dobra largura e altura. As dimensões-base oficiais são múltiplos compatíveis com o modelo e produzem oito pixels excedentes em um eixo para os formatos Full HD usuais:

| Formato | Etapa 1 | Saída bruta 2× | Entrega |
|---|---:|---:|---:|
| 16:9 | 960×544 | 1920×1088 | crop central 1920×1080 |
| 9:16 | 544×960 | 1088×1920 | crop central 1080×1920 |
| 1:1 | 544×544 | 1088×1088 | crop central 1080×1080 |

O crop é determinístico e ocorre depois da geração; ele não muda seed ou conteúdo latente.

## Frames, FPS e duração

O LTX-2.5 exige `1 + múltiplo de 8` frames. O orquestrador calculará o número válido mais próximo para a combinação de FPS e duração pedida e registrará a duração efetiva no manifest.

O padrão inicial é 24 fps e 5 segundos, que nos workflows oficiais aparece como 121 frames. Durações maiores elevam VRAM e tempo; cenas longas devem ser divididas quando isso preservar narrativa e consistência.

## Prompt enhancement

O workflow oficial pode expandir o prompt com um segundo Gemma. Neste projeto ele começa desligado porque:

- o agente já prepara um prompt cinematográfico detalhado;
- usar exatamente o prompt registrado melhora a reprodutibilidade;
- evita carregar outro text encoder durante a prévia;
- impede uma reescrita inesperada de detalhes de marca ou continuidade.

Se for habilitado no futuro, o texto expandido e a seed do enhancer também devem ser armazenados no manifest do job.

## Estrutura recomendada do prompt

1. enquadramento e linguagem cinematográfica;
2. cenário, luz, paleta, textura e atmosfera;
3. personagem/objeto com identificadores constantes;
4. ação em ordem cronológica;
5. movimento de câmera em relação ao sujeito;
6. som ambiente, música e diálogo entre aspas, quando houver.

Para continuidade, cenas diferentes devem reutilizar literalmente os descritores de identidade, figurino, produto, cenário e iluminação.

## Fontes primárias

- <https://github.com/Lightricks/ComfyUI-LTXVideo/tree/master/example_workflows/2.5>
- <https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Ingredients>
- <https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Motion-Track-Control>
- <https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Union-Control>
- <https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-In-Outpainting>
- <https://docs.ltx.io/open-source-model/usage-guides/two-stage-generation>
- <https://docs.ltx.io/open-source-model/usage-guides/prompting-guide>
