# Robô DOE-AL → PMAL

Sistema automático que, nos dias úteis, baixa a edição do Diário Oficial do
Estado de Alagoas, extrai a seção da **Polícia Militar de Alagoas (PMAL)** e as
menções à PMAL em outras seções, e publica tudo compilado numa página web.

Custo: **zero**. Roda inteiramente no GitHub (Actions + Pages).

---

## Instalação (uma única vez, ~10 minutos)

### 1. Conta e repositório
1. Crie uma conta gratuita em [github.com](https://github.com) (se ainda não tiver).
2. Clique no **+** (canto superior direito) → **New repository**.
3. Nome sugerido: `doe-pmal`. Marque **Public**. Clique em **Create repository**.

### 2. Enviar os arquivos
1. Na página do repositório recém-criado, clique em **uploading an existing file**.
2. Arraste para a janela **todo o conteúdo** desta pasta (os arquivos e as pastas
   `.github` e `docs` juntos — o GitHub preserva a estrutura).
3. Clique em **Commit changes**.

> Se a pasta `.github` não aparecer no seu computador, ative "mostrar arquivos
> ocultos" no explorador de arquivos (no Windows: guia Exibir → Itens ocultos).

### 3. Ligar o site (GitHub Pages)
1. No repositório, vá em **Settings → Pages**.
2. Em **Branch**, escolha `main` e a pasta **/docs**. Clique em **Save**.
3. Em 1-2 minutos seu site estará no ar em:
   `https://SEU_USUARIO.github.io/doe-pmal/`

### 4. Ligar o robô (GitHub Actions)
1. Vá na aba **Actions** do repositório.
2. Se aparecer um aviso, clique em **I understand... enable them**.
3. Clique no workflow **Robô DOE-AL PMAL** → botão **Run workflow** → **Run workflow**
   (isso executa a primeira compilação na hora, sem esperar o horário agendado).
4. Aguarde ~2 minutos e atualize o site: a edição do dia já deve aparecer.

Pronto. A partir daí o robô roda sozinho às **9h, 12h e 16h (horário de Maceió),
de segunda a sexta**, e o site se atualiza automaticamente.

---

## Como funciona

- As edições do DOE-AL são publicadas com um **ID numérico sequencial** na API da
  Imprensa Oficial (`.../apinova/api/editions/downloadPdf/ID`).
- O robô guarda em `state.json` o último ID conhecido e, a cada execução, sonda os
  30 IDs seguintes. Encontrou PDF novo → baixa, extrai o texto (respeitando as duas
  colunas do diário), recorta a seção da PMAL, separa as matérias pelo marcador
  "Protocolo" e varre o restante do diário atrás de menções (PMAL, PM/AL, postos
  e graduações: Cel PM, Maj PM, Sgt PM etc.).
- O resultado vira páginas HTML em `docs/`, servidas pelo GitHub Pages, com campo
  de busca para filtrar por nome, posto ou matrícula.

## Ajustes comuns

- **Horários**: edite `.github/workflows/robo.yml` (lembre: os horários são em UTC;
  Maceió = UTC-3).
- **Termos monitorados**: edite a lista `MENTION_TERMS` em `scraper.py`.
- **Rodar na hora**: aba Actions → Robô DOE-AL PMAL → Run workflow.

## Limitações honestas

- Se a Imprensa Oficial mudar a estrutura do site/API, o robô precisará de ajuste.
- Edições cujo PDF seja imagem escaneada (raro nas edições atuais) não têm texto
  extraível.
- A compilação é informativa; o documento oficial é sempre a edição certificada
  digitalmente no site da Imprensa Oficial Graciliano Ramos.

---

## Receber o PDF compilado por e-mail (já configurado para aldersouza@gmail.com)

O robô já está configurado para enviar o PDF compilado para **aldersouza@gmail.com**
usando essa mesma conta como remetente. Falta só UM passo: dar ao robô uma
"senha de app" do seu Gmail (é uma senha especial, separada da sua senha normal,
que pode ser revogada a qualquer momento).

### Passo único: criar a senha de app e cadastrar no GitHub

1. Ative a **verificação em duas etapas** na sua conta Google (se ainda não tiver):
   [myaccount.google.com/security](https://myaccount.google.com/security)
2. Acesse [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. Dê um nome (ex.: "Robo DOE") e clique em **Criar**.
4. Copie a senha de 16 letras que aparecer (ignore os espaços).
5. No repositório do GitHub: **Settings → Secrets and variables → Actions →
   New repository secret**. Nome: `MAIL_PASSWORD` — Valor: a senha de 16 letras.
   Clique em **Add secret**.

Pronto. Na próxima edição nova, o e-mail chega em aldersouza@gmail.com com o
PDF compilado em anexo, assunto "DOE-AL — Clipping PMAL de hoje".

- Sem o secret cadastrado, o robô continua funcionando (site + PDFs) — só não envia e-mail.
- Para trocar o e-mail ou incluir mais destinatários, edite a linha `EMAIL_ROBO`
  (remetente) e a linha `to:` em `.github/workflows/robo.yml` — em `to:` pode
  colocar vários e-mails separados por vírgula.
