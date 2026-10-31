# Radar hot — textos quentes do nicho e ideias mais fortes (2026-10-31)

## Por quê
O dono monetiza contas de texto +18 no X para público masculino brasileiro. O Radar de trend acha o assunto,
mas não mostra **quais textos do próprio nicho estão engajando**, e as ideias de post vinham repetidas
(mesma frase para todo assunto da mesma categoria, sempre na mesma ordem) e leves demais.

## O que entra
1. **Textos quentes do nicho** (card novo na aba Radar)
   - "Caçar textos quentes" roda o ator do Apify com buscas do nicho (sintaxe de busca do X: `lang:pt`,
     `min_faves`, `-filter:replies`, `-filter:links`) e guarda numa biblioteca compartilhada (`hot_texts`).
   - Cada texto ganha um **formato** (promessa, enquete, pergunta polêmica, desafio, lista, confissão,
     provocação) com "por que funciona"; filtro por formato; nota = curtidas + 2·RT + 3·respostas + views/200.
   - Fora da biblioteca: menor de idade / sem consentimento / incesto (`UNSAFE_RE`), chamada para link, bio,
     grupo, Telegram, privado, Pix, VIP (`CTA_RE`), repost, resposta, texto curto.
   - Uma caçada vale 2 h para todos (`HUNT_GAP`): clicar antes devolve a biblioteca sem gastar crédito.
   - Biblioteca: 45 dias, no máximo 600 textos (os de nota mais alta ficam).
   - "Recriar com Grok" num texto: 6 versões novas no mesmo formato, sem copiar.
2. **Ideias de post mais fortes**
   - Seletor **Leve | Quente** na aba (`localStorage.radar_heat`, padrão Quente); a API aceita `heat=leve|quente`
     (padrão `leve`, compatível com o que existia).
   - Quente: modelos novos por tipo de assunto no tom dos exemplos do dono (promessa condicional, pergunta
     polêmica aos homens, desafio, enquete, duplo sentido) + perguntas quentes sem assunto ("Pergunta quente").
   - Sem repetição: a lista é sorteada por assunto (semente = norm + `seed`), com um formato diferente por
     linha antes de repetir formato; "Mais ideias" pede outra semente.
   - Grok: pedido reescrito com o nível, os formatos, os exemplos do dono e os 6 melhores textos da biblioteca
     como referência; 8 linhas, cada uma num formato.
3. **Trends para o nicho**: cada assunto ganha `angle` (melhor formato: promessa quando há vencedor — jogo,
   eleição, luta, paredão; enquete para "A x B"; pergunta no resto) e bônus de polêmica no "Bom pra engajar".
4. **Custo explícito**: todo botão leva um selo — `Grátis`, `Apify · crédito` ou `Grok · xAI`.

## Regras fixas (não mudam com o nível)
- Nunca chama para link, bio, grupo, Telegram, canal, privado (memória do dono, teste fixo).
- Nunca sexualiza pessoa real com nome: o assunto é a condição ("se o Lula ganhar…"), o conteúdo é sobre quem posta.
- Nada com menor de idade, sem consentimento ou incesto; assunto bloqueado do radar não ganha ideia.

## Fora
- Sem coleta automática em segundo plano (só no clique, gasta crédito de quem clicou).
- Campos de saída do ator na busca avançada ainda não conferidos numa execução real.
