# doc-harbor-server

**Självhostad dokumenthanterare** med filträd, versionshistorik, Markdown- och HTML-redigering, Kanban-tavlor, att-göra-listor, grupper, backup och ett REST-API – i en enda Docker-container.

🌐 Webbplats: **https://doc-harbor.sandgren.one** · 📖 [Instruktioner](https://doc-harbor.sandgren.one/instruktioner) · 🆕 [Nyheter](https://doc-harbor.sandgren.one/nyheter)

*English below.*

---

## Snabbstart

Du behöver en Linux-server med Docker och Docker Compose.

```bash
mkdir doc-harbor && cd doc-harbor
U=https://raw.githubusercontent.com/walker42195/doc-harbor-server/main
curl -fsSLO $U/docker-compose.yml
curl -fsSL $U/.env.example -o .env
sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$(openssl rand -hex 32)/" .env
docker compose up -d
```

Öppna sedan **http://din-server:8090/setup** och välj lösenord för kontot `admin`. Gör det direkt – tills ett admin-konto finns kan vem som helst som når sidan välja lösenordet.

Allt sparas i mappen `./data`: filer, att-göra-listor, Kanban-tavlor och databasen (`.doc-harbor.db`).

### HTTPS

Kör doc-harbor bakom en reverse proxy med HTTPS, till exempel Caddy, nginx, Traefik eller Cloudflare Tunnel, och peka den mot port 8090. Exempel med Caddy:

```
docs.example.com {
    reverse_proxy localhost:8090
}
```

### Uppgradera

```bash
docker compose pull && docker compose up -d
```

Admin ser en notis i webbappen när en ny version finns (se *Integritet* nedan).

### Backup

Under **💾 Backup** i webbappen kan varje användare exportera sina filer, att-göra-listor och Kanban-tavlor. Admin kan också ta en fullständig backup av hela instansen och återställa den i en ny, tom instans. Du kan även säkerhetskopiera mappen `./data` direkt.

## Inställningar (`.env`)

| Variabel | Standard | Beskrivning |
|---|---|---|
| `SECRET_KEY` | – | **Krävs.** Hemlig nyckel för inloggningen (`openssl rand -hex 32`). |
| `ADMIN_PASSWORD` | tom | Lämna tom. Om den är satt skrivs admin-lösenordet över varje gång en ny image startas – bara till för att låsa upp ett konto. |
| `REGISTRATION_ENABLED` | `false` | Låt vem som helst skapa ett konto via e-postverifiering på `/register`. |
| `SMTP_HOST`, `SMTP_PORT` | `localhost`, `25` | E-postserver för registrering och gruppinbjudningar. |
| `FROM_EMAIL` | `noreply@localhost` | Avsändaradress för e-post. |
| `UPDATE_CHECK` | `true` | Anonym uppdateringskoll, se nedan. |

## Integritet: uppdateringskollen

En gång per dygn frågar servern `https://doc-harbor.sandgren.one/server-version.json` om det finns en nyare version, så att admin kan få veta det. Anropet innehåller **bara**:

- `id` – ett slumpat UUID som skapas första gången servern startar, och
- `v` – serverns versionsnummer.

Inget om användare, filer eller innehåll skickas, och svaret används bara för att visa notisen. Vi sparar inte IP-adresser för de här anropen; de används bara för att räkna hur många instanser som är igång. Stäng av med `UPDATE_CHECK=false`.

## Appar

Det finns gratis appar för **Android, Linux och Windows** som ansluter till din server. De laddas ner från [doc-harbor.sandgren.one](https://doc-harbor.sandgren.one). Apparna är inte öppen källkod och ingår inte i det här repot.

## REST-API

Allt finns under `/api/` med Basic Auth eller Bearer-token (`POST /api/auth/login`). Exempel:

```bash
curl -u admin:lösenord https://docs.example.com/api/browse/
curl -u admin:lösenord -X POST -H "Content-Type: text/plain" --data-binary @rapport.md \
     https://docs.example.com/api/file/Rapporter/rapport.md
```

Fler exempel finns i [instruktionerna](https://doc-harbor.sandgren.one/instruktioner).

## Bygga själv

```bash
git clone https://github.com/walker42195/doc-harbor-server.git
cd doc-harbor-server
cp .env.example .env   # fyll i SECRET_KEY
sed -i 's#^    image: .*#    build: .#' docker-compose.yml
docker compose up -d --build
```

## Licens

Copyright © 2026 Fredrik Sandgren.

doc-harbor-server är fri programvara under **GNU Affero General Public License v3.0** ([LICENSE](LICENSE)). Du får använda, ändra och dela den. Gör du ändringar och låter andra använda din version över nätverket ska du också dela källkoden till ändringarna.

Säkerhetsproblem rapporteras enligt [SECURITY.md](SECURITY.md). Bidrag är välkomna, se [CONTRIBUTING.md](CONTRIBUTING.md).

---

## English

**doc-harbor-server** is a self-hosted document manager with a folder tree, version history, Markdown and HTML editing, Kanban boards, todo lists, groups, backup and a REST API – all in a single Docker container.

**Quick start:** download `docker-compose.yml` and `.env.example` (see the commands above), set `SECRET_KEY` in `.env` (`openssl rand -hex 32`), run `docker compose up -d`, then open `http://your-server:8090/setup` right away to choose the `admin` password. Data is stored in `./data`. Put it behind a reverse proxy with HTTPS. Upgrade with `docker compose pull && docker compose up -d`.

**Settings:** see the table above. Self-registration is off by default (`REGISTRATION_ENABLED=false`); leave `ADMIN_PASSWORD` empty.

**Privacy:** once a day the server asks `https://doc-harbor.sandgren.one/server-version.json` whether a newer version exists, so admins can be notified. The request contains only a random instance ID and the version number – nothing about users, files or content. IP addresses are not stored for these requests. Disable it with `UPDATE_CHECK=false`.

**Apps:** free apps for Android, Linux and Windows are available at [doc-harbor.sandgren.one](https://doc-harbor.sandgren.one). The apps are not open source and are not part of this repository.

**License:** GNU Affero General Public License v3.0. Security issues: see [SECURITY.md](SECURITY.md).
