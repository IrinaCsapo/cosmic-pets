# Cosmic Pets — LinkedIn Launch Posts
### Monday–Friday, 5 days

---

## Monday — The Origin Story
**"Why I built this"**

I've spent years making cosmic pet portraits by hand.

Every single one — 20 to 50 real photo cutouts, layered in Adobe Illustrator, composited until it felt like magic. It takes days. It's obsessive. And I absolutely love it.

But I kept thinking: what if someone could feel even a glimpse of that magic instantly? What if they could see their pet in a cosmic world in seconds — not days?

So I built Cosmic Pets. ✦

It's an AI portrait generator that turns your pet photo into a surreal cosmic collage — with hand-styled vibes I designed myself. Midnight Gothic. Secret Garden. Electric Universe. Ocean Deep. Each one a world I art-directed from scratch.

I'm a product designer, not an engineer. I've never built a web app before. I had zero experience with AI image pipelines, servers, or payment systems.

I built it anyway — with a lot of help, a lot of trial and error, and more joy than I expected.

This week I'm sharing the whole messy, beautiful journey. What inspired it. How I built it. What broke. What surprised me. And why I think the future of creative tools is going to be wild.

Day one starts now. 🚀

Try it at cosmicpets.love — your pet deserves a portal.

---

## Tuesday — The Influences
**"The artists and worlds that shaped every pixel"**

Every vibe in Cosmic Pets has a visual reference behind it. I didn't invent these worlds — I was inspired by them.

Félix de Boeck, a Belgian avant-garde painter from the early 20th century, gave me the geometric grid lines you see in the Electric Universe vibe — fine lines radiating from light sources, like cosmic blueprints. The man was painting futurism before futurism had a name.

Persepolis, the ancient Persian capital, gave me carved lamassu guardian statues — winged animal figures with feline bodies — emerging from the corners of the Midnight Gothic and Celestial Palace vibes. I wanted mythology and mystery, not generic sci-fi.

Real NASA astrophotography. False-colour geological mapping. Photographic collage artists. Surrealist assemblage. Organic forms cut out and layered like someone had access to every library on earth.

The brief I gave the AI wasn't "make something cosmic." It was hundreds of words of specific, art-directed instruction. The garland wrapping the portal. The exact palette of the moon surface. The species of birds. The type of stone columns.

Prompt engineering is, in many ways, the new art direction.

And I love it.

What are the artists or visual references that live rent-free in your head? I'd love to know. 👇

---

## Wednesday — The Process
**"How I built an AI image pipeline with no engineering background"**

Six months ago I didn't know what a server was. I mean — I knew what a server was. I just didn't know how to build one.

Here's how Cosmic Pets actually works under the hood, in plain English:

**1. You upload your pet photo.**
The server runs it through a background removal model (rembg) locally — no third-party API, fast and free.

**2. I generate the cosmic background.**
Using FLUX, an AI image model, the server sends my prompt — hundreds of words of art direction — along with reference images I painted myself. The model uses those references to maintain a consistent style across every portrait.

**3. Everything gets composited.**
Your pet is placed back into the generated world. Scaled. Masked with an oval. Positioned in the portal. The garland wraps around. The glow ring frames it. All done with Pillow (a Python image library) running on a tiny Railway server.

**4. You get your portrait.**
In about 10–15 seconds.

The whole stack: Python, Railway, Replicate, Stripe for payments, and a single HTML file for the frontend that I absolutely sweated over.

I could not have done this without AI assistance helping me write and debug the code. I want to be honest about that. But every single design decision, every prompt word, every vibe — that was me.

The tool does the heavy lifting. The vision is still human.

---

## Thursday — What Went Wrong
**"The bugs, the chaos, and what I learned"**

Let me tell you about the time my buy button silently did nothing.

I had Cosmic Pets running. The pipeline worked. The portraits were beautiful. I added a paywall — users get 3 free tries, then they buy a credit pack. I wired up Stripe. I tested it. The checkout button looked fine.

It did absolutely nothing.

Turns out the test price IDs I'd copied from Stripe were wrong — subtly, invisibly wrong. No error message. Just silence. I had to dig through server logs, add better error logging, and stare at API responses until I found it.

That's just one story.

There were also: human figures appearing in scenes that were supposed to have none (ruins kept generating full-body male statues no matter how many times I told the AI not to). A share button that sent a WhatsApp link that wasn't actually tappable. A pip counter showing "3 free left" even when you had zero. A reset URL that worked on desktop but not on iOS because of how GoDaddy handles redirects.

Every single one of these was fixable. None of them were catastrophic. But each one taught me something about how these systems actually behave versus how I assumed they would.

The biggest lesson: **test everything on the device your users will actually use.** Not just your laptop. Not just Chrome. The real thing.

What's the bug that taught you the most? 👇

---

## Friday — What Went Right
**"The moment I knew this was something special"**

There's a moment when you're building something where you stop fixing things and just... stare.

For me it was when the Midnight Gothic vibe started generating exactly what I'd imagined. Crows perched on the circular portal. Black roses and deep crimson peonies in the garland. Wrought iron candelabras. Lamassu guardian statues in the corners. A black hole with a crimson and deep violet accretion disk glowing in the distance.

I had written all of that into a prompt. The AI had understood every word of it. And it came out more beautiful than I expected.

That's when I knew.

This week I've shared the story, the influences, the process, and the chaos. But this is what I want to end on: the pure, ridiculous joy of watching your pet become a cosmic being.

The dachshund with its tongue out in Electric Universe. The fluffy white dog surrounded by maximalist Cosmic Abstract geometry. The grey cat in Ocean Deep with the space whale floating overhead. Every portrait is genuinely, unexpectedly stunning.

Cosmic Pets is live at **cosmicpets.love** — 3 free portraits to try, no account needed.

Go make something beautiful for your pet. Share it with me. I want to see every single one. 🐾✨

And if you want the real thing — a bespoke, hand-crafted collage portrait made by an actual human (me) — that lives at **cosmicpets.co.uk**.

Thank you for following this week. This is just the beginning. 🚀

---

*cosmicpets.love — AI cosmic pet portraits*
*cosmicpets.co.uk — Handmade bespoke commissions*
