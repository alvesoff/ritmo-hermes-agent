# Ritmo · especificação do produto

Agente genérico: serve para qualquer pessoa. Nada de uma pessoa específica entra no
código, no prompt ou na imagem. Os dados de cada dono vêm da conversa e ficam no
volume do agente dele.

## Papel

Assistente pessoal de rotina: planeja a semana, manda lembretes na hora certa pelo
iMessage e replaneja conforme o dia anda.

Módulo opcional, desligado por padrão: **Equipe**, para quem tem gente entregando
código. Revisa os PRs, faz o merge do que está certo e cria demanda nova para o que
não está.

## Base

- Plow, imagem `plow-hermes-agent` (agendamento nativo do Hermes). Sem Latch.
- Fala só pelo iMessage, com mensagens curtas, no idioma do dono.
- Só o dono dá comandos.

## Onboarding (primeira conversa, poucas perguntas)

Nome, fuso horário, horário de acordar e de dormir, janela de silêncio, compromissos
fixos, hábitos (com intervalo) e metas da semana. Tudo muda depois em linguagem
natural: "muda o lembrete de água para cada 3h", "não me manda nada no sábado".

O onboarding não fala do módulo Equipe.

## Módulo 1: Rotina e lembretes

1. Plano da semana no dia e horário escolhidos, com compromissos, calendários,
   hábitos, metas e o que sobrou da semana anterior. Manda o resumo e pede "ok" ou
   ajuste.
2. Bom dia: a agenda do dia em tópicos.
3. Lembrete na hora: "14h · estudar inglês (50 min)". Hábitos com intervalo
   ("beber água a cada 2h entre 8h e 22h").
4. Respostas rápidas: "feito", "adiar 30", "pular", "não vou conseguir". Replaneja o
   resto do dia e avisa só o que mudou.
5. Compromisso novo ou atraso: encaixa tudo de novo sem invadir descanso nem
   silêncio.
6. Fechamento do dia: o que foi feito, o que ficou e para onde foi.
7. Pedidos livres: "me lembra amanhã 10h de ligar pro contador", "o que tenho
   quinta?", "quero estudar 5h por semana".
8. Silêncio respeitado, exceto o que o dono marcar como urgente.

## Módulo 2: Equipe (opcional, desligado por padrão)

Aparece só no "ajuda" ou quando o dono fala de equipe ("quero acompanhar minha
equipe").

### Ativação, guiada pela conversa

1. **GitHub por código:** o agente manda um código, o dono digita em
   github.com/login/device e escolhe os repositórios. Nenhum token passa pela
   conversa.
2. **Pessoas:** nome, login do GitHub, repositórios e nível. Júnior recebe demanda
   passo a passo; sênior recebe demanda curta.
3. **Onde ficam as demandas:** Issues do repositório (padrão) ou outro quadro por
   conector.
4. **Regras de revisão, modelo de demanda, política de merge e de publicação:**
   começam no padrão e o dono ajusta falando.

### Revisão automática

1. Quando: horário fixo, PR novo ou atualizado (o agente confere de tempos em
   tempos), ou a pedido ("revisa o PR 52").
2. Liga o PR à demanda pelo link ("Closes #12"). Sem demanda ligada, pergunta ao
   dono em vez de chutar.
3. Confere cada critério da demanda, as regras do dono, a documentação, o CI e se há
   mudança fora do escopo pedido.

### Merge automático

Faz o merge sozinho quando tudo isto for verdade:

- todos os critérios e regras ok;
- CI verde (repositório sem CI: pede o ok do dono);
- sem conflito e sem mudança fora do escopo;
- branch de destino liberada para merge automático. Padrão: todas, inclusive a de
  produção. O dono pode restringir ("na main só com meu ok");
- respeita as proteções do próprio GitHub.

Depois do merge: método configurado (padrão: merge commit, que mantém um commit por
alteração), apaga a branch, fecha a demanda se configurado e avisa o dono:
"PR 52 (Ana) mergeado na main: 5 de 5 critérios ok".
"desfaz o PR 52" abre um PR de revert.

### Quando não está de acordo

1. Escreve a demanda nova no modelo do dono, no nível da pessoa. Modelo padrão:
   Objetivo / O que precisa ter / Entrega, com etapas e um pedido pronto para colar
   numa IA.
2. Publica sozinha no quadro (padrão) e avisa o dono com o link. O dono pode trocar
   para "só com meu aprovar": aí manda o rascunho e espera "aprovar",
   "ajusta: prazo sexta" ou "descartar".

### Administração, só pela conversa

- "equipe" mostra o resumo: pessoas, repositórios, regras, horários e políticas.
- Exemplos: "adiciona a Ana", "remove a Ana", "revisa às 18h", "pausa até segunda",
  "na main só com meu ok", "desliga a equipe" (apaga os dados se o dono pedir).
- Horário de revisão e prazos das demandas viram blocos e lembretes na rotina do
  dono.

### Limites fixos

Nunca fala com a equipe nem comenta no PR. Nunca escreve código, nunca força push,
nunca faz merge com CI vermelho e nunca mexe nas configurações do repositório.

## Conectores (plugáveis, um por fonte)

- Calendário: Google Agenda (pelo conector da própria Plow), Outlook, link ICS.
- Código: GitHub.
- Quadro de demandas: GitHub Issues (padrão) ou qualquer quadro com API.

## Tudo configurável

Horários, intervalos, silêncio, textos dos lembretes, regras de revisão, modelo de
demanda, política de merge e de publicação, e conectores. Cada dono tem os seus.
Nada fixo no código.

## Segurança

- Imagem pública, sem token, segredo ou dado pessoal.
- Os tokens de cada dono ficam só no volume do agente dele.
- GitHub App com o mínimo: ler código e CI, mergear PR, criar issue. Sem admin.

## Fases

1. Login na Plow e Hermes puro respondendo no iMessage.
2. Onboarding, rotina e lembretes.
3. Equipe: revisão e demanda.
4. Equipe: merge automático.
5. Conectores de calendário.
6. Agent Index e 1-click deploy.
