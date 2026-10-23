# How the walk works

`joern/walk.sc` takes a role mapping and returns the order those roles interact.
A role is a name the annotation attached to an entity in the code; an
**interaction** is one directed edge `A → B`, meaning a run of this code got
from A to B. This document is how those edges are produced.

> **Status.** The live path. Every consumer calls it through `detector.detect` —
> `secpat recover` stage 3 and `secpat evaluate`'s Level 3.
> Read **Known issues** before trusting a number.

**What it reads from the graph.** Only three things, and each is an edge the
frontend already resolved when it built the CPG: `CALL → callee` (what method a
call runs), `IDENTIFIER → REF` (what declaration a name refers to), and
`CFG` (what runs next). Nothing else about the graph matters here.

---

## The example

```python
# duffy/admin.py
class AdminContext:
    def proxy_controller_function(self, controller_function, **kwargs):   # 50
    def get_tenant_id(self, name: str):                                   # 53
    def show_tenant(self, name: str):                                     # 62
        return self.proxy_controller_function(                            # 63
            tenant.get_tenant, id=self.get_tenant_id(name))
```

```json
{"role": "Enforcer", "entity": "show_tenant",               "lines": "62"}
{"role": "Proxy",    "entity": "proxy_controller_function", "lines": "50"}
{"role": "Resolver", "entity": "get_tenant_id",             "lines": "53"}
```

```
1. Enforcer → Resolver   admin.py:63   call
2. Enforcer → Proxy      admin.py:63   call
```

Both from line 63, **Resolver first** — `self.get_tenant_id(name)` is an
*argument* to `proxy_controller_function`, so it runs before the call that
receives it. Nothing sorts that; it falls out of the graph.

---

## 1. Citations become nodes

The mapping is text; the graph has nodes. Each citation is looked up:

```
"show_tenant" + file + line 62   ──▶  METHOD #4102 → Enforcer
```

The cited line is the first number of `lines`, and any dash separates a range:
a citation may be written `374–415` with an en dash, and until 2026-09-26 only `-`
was read, so 53 rows in 28 of 39 ground-truth mappings lost their line and fell to
`first-of-many` -- on `Athroniaeth_keyshield` that anchored `_verify_key` and
`get_by_key_id` on the abstract declarations instead of the cited implementations.

Keep every declaration in the repo with that exact name — method, class,
parameter, local, field. Prefer those in the cited file. Then pick one, trying
four readings in order, reported per role as `by`:

| reading | |
| --- | --- |
| `declaration-at-line` | something declared on that line |
| `occurrence-at-line` | something *used* on that line, followed back through its `REF` edge |
| `unique-name` | only one candidate exists — the line is never consulted |
| `line-tiebreak` | several candidates, the line chooses |

There is no nearest-match fallback: a citation matching nothing leaves the role
**unlocated**, which is a disagreement between the annotation and the graph and
should stay visible. A citation naming an entity outside the repo becomes an
**external** role, matched later by callee name and keyed on `(citing file,
name)`.

The result is one map, `roleOfNode`, and it is the only thing the walk consults.
A role cited on a **class** is also recorded against every method that class
declares, so a call landing on any of them counts as reaching the role — unless
some role cited one of those methods directly, which is the more specific claim
and wins. A role cited on a **field** is likewise recorded against the accessors
the language generates for it (`LangSpec.accessorsOf`), and those beat the
enclosing class for the same reason.

---

## 2. Where the walk starts

Two questions, in this order.

**Which role.** The walk runs **once**, from one role; every other role is
*reached*, never started at. That role must be a **root** — one no other cited
role reaches, counting calls and framework wiring alike. The pattern's first
speaker is used if it is a root (`entryRule: behaviour`); otherwise the root
reaching the most other roles wins (`code-root`). **No root means no order**: a
cycle has no beginning, and a derivation that picked one anyway would be
indistinguishable in the output from one the code really starts with.

**Where in that role.** One rule, two shapes — *start at whatever the cited
thing runs next*:

| cited on | start node |
| --- | --- |
| a method (`METHOD`) | `cfgFirst`, the body's first node |
| a class (`TYPE_DECL`) | `cfgFirst` of **every** method it declares, each walked in turn |
| a statement (a role on a variable) | that line's first node, inside the enclosing body |

A class is started from all its bodies because that is the set the root test
above judged it on (`reachesFrom` reads every body). Starting from the first
alone -- which it used to -- walked whichever method the graph listed first: on
`cloudfoundry_uaa` the Enforcer cited as `OAuth2AuthenticationProcessingFilter`
began in a setter, never entered `doFilter`, and derived nothing, while the same
mapping citing `doFilter` derived three interactions.

Naming a function is naming its body. Naming a variable is naming one statement,
and execution simply carries on from it — so a module-level
`auth_scheme = FastApiKeycloack(...)` reaches `__init__` with no special case,
because the constructor call on that line is an ordinary CALL node the walk
arrives at. Neither shape needs a stop rule. A `PARAM` has no body, so a role on
a parameter is only ever reached.

---

## 3. Stepping

*`Walker.walkBody` in `walk.sc`; its traversal loop is the `while (pending.nonEmpty)` at its end.*

The walk moves in **two different ways**, and it matters that they are different:

```
within a method    follow a CFG edge        "what runs next here"
into another       follow a callee edge     "which method does this call run"
                   and start a new loop
```

A CFG belongs to **one method**. There is no CFG edge from a caller into a
callee, so following CFG edges alone would never leave the body you started in.
Crossing is a separate move, with a separate edge and a fresh traversal — how the
other end of that edge is found has its own section, **Locating the method to
continue in**, because it is where most of what the derivation misses is lost.

### The whole flow, once

Start in `show_tenant` (the `Enforcer`). Read top to bottom — this is every
movement the walk makes:

```
walkBody(show_tenant)                        ← loop #1 begins at its cfgFirst
  node  1..12   identifiers, field accesses  ← stepped past, nothing to do
  node 13       CALL self.get_tenant_id(name)
                │  EMIT  Enforcer → Resolver
                │  callee edge ──▶ get_tenant_id
                └─ walkBody(get_tenant_id)   ← loop #2, its OWN pending + visitedNodes
                      line 54 → 55 → 56 → 55 → 54   (whole body)
                      nothing here is a role → nothing emitted
                   ◀── loop #2 ends, control returns into loop #1
  node 14       CALL self.proxy_controller_function(…)
                │  EMIT  Enforcer → Proxy
                └─ walkBody(proxy_controller_function)   ← loop #3
                   ◀──
  node 15       RETURN
  node 16       METHOD_RETURN                ← loop #1 ends
```

Three points to take from that:

**Loop #1 never left `show_tenant`.** Its CFG successor from node 13 is node 14,
in the same method — verified in the graph:

```
CALL  self.get_tenant_id(name)
  ├─ CFG successor  →  self.proxy_controller_function(…)   inMethod: show_tenant
  └─ callee edge    →  AdminContext.get_tenant_id          body starts at L54
```

Stepping the CFG goes *over* the call. The only way in is the callee edge.

**Loop #2 is a genuinely separate traversal.** `walkBody` is recursive, and each
call makes its own `pending` stack and its own `visitedNodes` set. Nothing is
shared with the caller's loop.

**Nesting comes from that recursion, not from the stack.** Loop #2 runs to
completion *while node 13 is still being inspected*, so everything found inside
`get_tenant_id` lands between interaction 1 and interaction 2 — the way a
sequence diagram draws a nested call. `pending` has nothing to do with it.

So the repo's control flow is thousands of small islands, one per method, and
`callee` edges are the only bridges:

```
   show_tenant              get_tenant_id            sync_session_maker
  ┌───────────┐            ┌───────────┐            ┌───────────┐
  │ ○→○→○→○  │            │ ○→○→○    │            │ ○→○      │
  └─────┬─────┘            └───────────┘            └───────────┘
        └──── callee ──────────▲
```

**And that is why an unresolved call hurts so much.** A missing `callee` edge is
a missing bridge: the island is still in the graph, and nothing can reach it. See
**Known issues** for how much of the graph that cuts off.

---

Now the inside of one loop.

```scala
pending.push(start)
while (pending.nonEmpty) {
  val n = pending.pop()
  if (visitedNodes.add(n.id)) {
    …                                                   // inspect this node (§4)
    n._cfgOut.collect { case c: CfgNode => c }.toList.reverse.foreach(pending.push)
  }
}
```

### `pending` — the to-do list

Nodes reached but not yet inspected. You need it only because a node can have
**more than one** successor; with one successor each, the loop would be a
variable:

```scala
var n = start
while (n != null) { inspect(n); n = n.next }      // no collection needed
```

**A stack, not a queue**, and that choice is what makes the order mean anything:

```
cond ─┬─▶ A1 ─▶ A2 ─▶ A3 ─┬─▶ join ─▶ ...
      └─▶ B1 ─────────────┘
```

| | visit order |
| --- | --- |
| **stack** (last in, first out) | `cond, A1, A2, A3, join, …, B1` |
| **queue** (first in, first out) | `cond, A1, B1, A2, join, A3, …` |

A queue steps every path forward together, interleaving `A1, B1, A2` — nodes
from two branches that can never both run. A stack follows one path to its end
before backing up, so each execution path comes out contiguous, which is what
running a program looks like.

### `visitedNodes` — why it stops

`add` returns true only the first time a node is seen, so each node is inspected
once and a loop (whose last node points back at its first) terminates. It is also
why the second arm of a branch stops as soon as it rejoins the first.

### `.reverse` — why the branches come out in source order

Two facts collide.

**Where a branch forks is not the `if`.** A `CONTROL_STRUCTURE` node has no CFG
successors at all. Control forks at the **condition**, whose two successors are
listed in source order, then-branch first. From the real CPG of
`duffy/app/auth.py:_req_tenant`:

```
CALL  L21  not credentials      ->  L22  optional      (then: the nested if)
                                ->  L27  tenant_name   (fall-through, after the if)
CALL  L22  not optional         ->  L23  HTTPException (then: raise)
                                ->  L25  None          (else: return None)
```

**A stack is last-in-first-out.** Push that list as it comes and the *second*
successor ends up on top, so the else-branch runs first:

```
without .reverse                        with .reverse
──────────────────────────────          ──────────────────────────────
push L23  stack: [23]                   reversed → [L25, L23]
push L25  stack: [23, 25]  ← on top     push L25  stack: [25]
pop       → L25   ✗ else first          push L23  stack: [25, 23]  ← on top
                                        pop       → L23   ✓ then first
```

**Measured on that method** — same walk, same graph, `.reverse` the only
difference, visit order by line:

```
with .reverse      21 21 22 22 23 23 23 23 27 27 … 34 … 37 … 40 40 17 25 25
without .reverse   21 21 27 27 … 40 40 17 38 38 35 35 34 … 22 22 25 25 23 23
```

The second is not a small perturbation: the whole body runs backwards — 38
before 35 before 34 — and the `raise` on line 23 is visited last of everything.
The emitted rank **is** the visit order, so every derived order in a branching
method would be scrambled.

**One consequence that surprises people.** Depth-first means the walk finishes
the first branch *and everything downstream of the join* before coming back for
the second. In the correct trace above, after line 23 it goes to 27 and runs to
the end of the method (40, then `METHOD_RETURN` at 17) — and only then picks up
25, the else-arm, dead last. At runtime only one arm runs, so there is no true
ordering between them; what the walk guarantees is that **both are seen** and the
source-first one is seen first.

### Iterative, not recursive

*Within* a body. The obvious way to write depth-first is a recursive function,
and that overflows the JVM stack — a CFG path is as long as the method is, one
frame per node, measured as a `StackOverflowError` on the first real annotated
repo. An explicit `pending` stack gives the same visit order with a constant
number of frames. (Crossing *between* bodies is recursive, and that is fine: the
depth there is the call chain, not the statement count, and `MAX_DEPTH` guards
it.)

### The nodes, in one line of source

`show_tenant`'s sixteen CFG nodes for line 63, in visit order:

```
step  line  label              code
   1    63  IDENTIFIER         self
   3    63  CALL               self.proxy_controller_function     ← <operator>.fieldAccess, skipped
  10    63  CALL               self.get_tenant_id                 ← <operator>.fieldAccess, skipped
  13    63  CALL               self.get_tenant_id(name)           ✔ EMIT → Resolver
  14    63  CALL               self.proxy_controller_function(…)  ✔ EMIT → Proxy
  16    62  METHOD_RETURN      RET
```

Identifiers are stepped past and `<operator>.*` calls filtered out by name
(`isRealCall`). **The order is not computed** — step 13 precedes step 14 because
the CFG edge points that way: `self.get_tenant_id(name)` is an *argument* to
`proxy_controller_function`, and an argument is evaluated before the call that
receives it. There is no sort anywhere in the script.

---


## 4. Emitting an interaction

**The whole rule:**

> While walking inside role **A**, traverse to something carrying role **B**.
> That is an interaction: emit `A → B`, and continue with the context now set to
> **B**.

```
context = A                 context = B
┌──────────────┐            ┌──────────────┐
│  A's code    │ ──edge──▶  │  B's code    │      EMIT  A → B
└──────────────┘            └──────────────┘      then carry on inside B
```

**Which code does what**, so each clause of that sentence can be read against
the file:

| clause of the rule | responsible | `walk.sc` |
| --- | --- | --- |
| "while walking inside role A" | `walkBody`'s `sources` parameter | 425 |
| "traverse to something" | the node match in `walkBody`, via `calleeOf` / `methodsPassedTo` / `referencedMethodInRepo` / `_refOut`; plus `Lang.entryEdges` before the first node | 489–513, 474 |
| "carrying role B" | `emitAndDescend` → `roleOfNode` + `externalRoles` | 444 |
| "B ≠ A" | `pairsFor` (drops self-pairs) | 318 |
| **"emit `A → B`"** | `emitAndDescend` — **the only `edges +=` in the file** | **449** |
| "continue with the context set to B" | `emitAndDescend`'s recursive `walkBody` | 450–451 |

Everything below is that rule, with the three things it leaves unsaid.

### The context is `sources`

`Walker.walkBody(m, from, sources, via, depth)`.

Set when a body is entered, constant while inside it, restored when the walk
returns. The pop is the JVM call stack — there is no separate structure.

### "Traverse to" means follow an edge

The `n match { … }` block inside `Walker.walkBody`, plus the
`Lang.entryEdges` call at `474`, which runs before the first node is stepped.

Roles live on **declarations** (METHOD, TYPE_DECL, LOCAL, PARAM, MEMBER) and the
walk steps on **executable** nodes (CALL, IDENTIFIER, …). Those two sets are
disjoint — measured on `CentOS_duffy`, the walk steps on 72,147 nodes, 28,395
nodes can carry a role, and the overlap is **zero**. So the walk never lands
*on* a role; it lands on a node that *points at* one, and traversing means
following that pointer. These pointers exist:

| at this node | edge followed | mechanism |
| --- | --- | --- |
| `CALL` | `c.callee` — the method it runs | `call` |
| `CALL` whose callee has no body | the repository's overrides of it | `dispatch` |
| `CALL` a framework passes on | a `Lang.routes` target — what the framework runs next | `dispatch` |
| `CALL` on a type the repository provides | the provider method (`Lang.providerAnnotation`) — what the framework injected | `dependency-injection` |
| a `METHOD_REF` in that call's arguments | `mr.referencedMethod` — a callable handed over | `route-registration` |
| a `METHOD_REF` no call takes — a nested `def`, a returned or assigned lambda, a method reference | `mr.referencedMethod` — the code the body hands on | `nested-definition` |
| `IDENTIFIER` | its `REF` edge — the declaration it names | `data` |
| *(no node)* | `Lang.entryEdges` — what the parameter defaults bind | `dependency-injection` |

Each one reads an answer the frontend already computed, except the two framework
rows, which read one the profile declares; none searches by name. Those two and
`dispatch` are tried only when the call's own callee has no body — see
**Framework routes** under §4's *Locating the method to continue in*.
Two timing rules: arguments are followed **before** the call that receives them,
because that is when they are evaluated; and an entry binding's
`dependency-injection` is emitted before the body's first node, because a
parameter is bound before the body runs — and only when the walk really enters
the method, never when it started partway down a body it does not own. A
provider's `dependency-injection` is emitted at the call made on the injected
object, with that call's other targets.

### What if it carries **no** role?

The `targets.isEmpty` branch of `emitAndDescend` (inside `Walker.walkBody`).

Your rule says nothing about this, and it is the case that makes the rule
productive. Roles are architectural and are normally separated by ordinary
helper functions, so:

```
A  →  helper  →  B
      (no role)
```

If the walk stopped at `helper`, it would never see `B` and `A → B` would be
lost. So: **no role → emit nothing, keep the context, keep going**, with the
helper appended to `via`. That is the only addition to your rule, and `via` is
what distinguishes a direct call from one found six helpers away.

A node carrying the **same** role emits nothing and the walk does not enter it.

### What if it carries **several** roles?

`Ownership.pairsFor`, with the ordering from `Ownership.appearanceOf`.

One entity can be several roles at once — `Comparator`, `Hasher` and `Encrypter`
all cited on `pwd_context.verify` is a deliberate claim that the one call is all
three. So both sides are lists, and **every pair is emitted**:

```
A → B, C, D          ordered by each role's first appearance in the pattern
```

The pattern only *orders* them; it never selects among them, because selecting
would discard a true interaction. Measured on `CentOS_duffy` with `Hasher` and
`Validator` both cited on `bcrypt.checkpw`:

```
pattern:  Enforcer step 1 · Validator step 2 · Hasher step 3
emitted:  Enforcer → Validator   then   Enforcer → Hasher
```

**Co-located roles on a library entity only ever land on the target side.** A
library entity has no body, so it is excluded from `withBody` (never an entry)
and from both descent sites by `filterNot(_.isExternal)` (never walked into): it
can only be reached. Roles co-located on a **repository method** are different
-- the walk enters that method as all of them, so `A, B → D` does happen
(`Enforcer` and `Verification manager` both cited as `login` emit
`Enforcer → Password store` *and* `Verification manager → Password store`).

**Between co-located roles: what the pattern prescribes** (`prescribedAmong`,
mechanism `co-located`). No call separates roles cited on one entity, so
nothing in the code emits an edge between them -- yet the citation's claim is
that the one entity plays each of them, and the pattern says how those roles
interact. So on reaching a node, every pair *among* the roles sharing it that
the walkthrough has as a step is emitted too, at the same site, ordered with the
arrival edges by the pattern. On `microcks_microcks`, with Verifier, Key manager
and Cryptographer all cited on `jwt(...)`:

```
Enforcer → Verifier          SecurityConfiguration.java:105  call
Enforcer → Key manager       SecurityConfiguration.java:105  call
Verifier → Key manager       SecurityConfiguration.java:105  co-located   (pattern step 3)
Enforcer → Cryptographer     SecurityConfiguration.java:105  call
Verifier → Cryptographer     SecurityConfiguration.java:105  co-located   (pattern step 4)
```

Only prescribed pairs: two co-located roles the pattern never connects get no
edge. The start node is reached by no call, so `Walker.coLocatedAtStart` emits
its prescribed pairs before its body runs. The label keeps these separable --
they are the pattern read at one site, not evidence of a call. Measured
2026-10-01 on a 15-record Java batch: microcks and epam gain `Verifier →
Cryptographer` on the recovery side (both cite the two on one `jwt` call);
their Level 3a is unchanged at 0.0, the other eleven scored records and the
PYTHONSRC regression set are identical.

**Not on a library call** (`Ownership.sameLibraryCall`, since 2026-10-04). Roles
whose citations all resolve to one *library* call -- `jwt(...)` above, Spring's
`httpBasic`, `@PreAuthorize` -- are still each reached at that call, but no
`co-located` pair is read between them: what passes between them happens inside
the library, where nothing shows which part plays which role, so the pattern's
step between them is not evidence of anything in this repository. The microcks
`Verifier → Key manager` and `Verifier → Cryptographer` rows above are no longer
emitted. This replaces `detector.SharedExternalEntity`, which refused such a
mapping outright and so cost the record its whole Level 3 -- untenable for a
recovery, whose citations cannot be corrected after the run. Co-location on a
repository method is unchanged.

### Where it happens

`emitAndDescend`, nested in `Walker.walkBody`.

All five arrivals call `emitAndDescend`, the only place in the file that appends
to `edges`. It resolves the target's roles, crosses them with the context, and
takes one of the three outcomes above:

```
different role  →  EMIT each pair, context ← target roles, via ← []
no role         →  no edge,        context unchanged,      via ← via :+ helper
same role       →  nothing
```

One row per pair: `from`, `to`, `file`, `line`, `via`, `mechanism`, `path`, `parent`.

`path` is the same trail as `via`, one `Step` per call instead of one name per
helper: `{caller, file, line, code, callee, mechanism, route?}`. It starts at
the call in the source role's body and ends at the call that reached the target,
so its last step is at the row's `file:line`. Each step keeps its own hop's
mechanism, while the row's `mechanism` summarises the trail (one `dispatch` hop
makes the whole row `dispatch`). `code` is the call's source text on one line,
and it is empty for a hop that no statement names (an entry binding or a field read).

`parent` is the `A → B` token of the interaction that entered the source role's
body (`walkBody`'s `enteredBy`), or null at the entry role. A `co-located` row's
parent is the pair, at the same call, that reached its source. Following
`parent` back from any row reaches the entry role. It names a token, not a
row, so it survives `firstOccurrences`. The parent's first occurrence is always
ranked before the child, because an edge is emitted before its target's body is
walked.

Both are evidence only. Neither feeds `keepMostRoles`' ranking or Level 3.

```
#2 Enforcer → Verification manager   dispatch   StandardAuthenticationProvider.java:23
   1. StandardLoginRequestFilter.attemptAuthentication → StandardAuthenticationProvider.authenticate
      StandardLoginRequestFilter.java:49  getAuthenticationManager().authenticate(authRequest)
      dispatch · [spring] ProviderManager.authenticate
   2. StandardAuthenticationProvider.authenticate → DefaultAuthenticationServiceImpl.authenticate
      StandardAuthenticationProvider.java:23  this.authService.authenticate(...)   call
#3 Verification manager → Password store   parent: Enforcer → Verification manager
   1. DefaultAuthenticationServiceImpl.authenticate → AuthenticationRepository.findByUsername
      DefaultAuthenticationServiceImpl.java:80  this.authenticationRepository.findByUsername(username)
```
*`hygieia_api`, a GPT recovery, 2026-10-02.*

---


## Locating the method to continue in

*`CallGraph.calleesOf` and `CallGraph.rescuedCallees`.*

§3 established that a CFG stops at the method boundary, so crossing into a callee
needs a different edge and a fresh traversal. This is how the other end of that
edge is found. It is the single most consequential mechanism in the walk: almost
every interaction the derivation reports depends on it, and almost everything the
derivation *misses* is it failing.

### The walk does not search. It reads the call's edges.

```scala
def calleesOf(c: Call): List[Method] = {
  val all = c.callee.l
  val inRepo = all.filterNot(_.isExternal).distinctBy(_.id)
  if (inRepo.isEmpty) rescuedCallees(c) match { case Nil => all.headOption.toList; case xs => xs }
  else if (inRepo.sizeIs == 1) inRepo
  else {                                   // a dispatch set
    val addressed = inRepo.filter(_.fullName == c.methodFullName).take(1)
    if (addressed.exists(hasBody)) addressed
    else addressed ++ inRepo.filter(hasBody).sortBy(_.fullName)   // (or one, if neither)
  }
}
```

It used to read **one** edge -- `c.callee.headOption` -- and that was a
cross-file bug, measured 2026-09-24. A call carries several edges more often
than it looks: a dynamically dispatched Java call has one per implementation
(chutney: 765 calls, 750 of them spanning files, and on 677 the first edge was a
bodiless interface method), and an untyped Python call can carry a library stub
beside the repository method (freqtrade: 138 calls where the stub came first, so
`worker.run()` lost `Worker.run`). The first edge is whatever storage order put
first, so the walk entered the wrong subclass in another file, an empty
interface body, or nothing:

```
CompareAction.java:34   compareExecutor.compare(logger, actual, expected)
  edges   CompareNotEqualsAction.compare   <- was taken, always
          AbstractCompareNumberAction.compare, CompareEqualsAction.compare,
          CompareExecutor.compare (no body), NoCompareAction.compare, ...
```

Now: the addressed method when it has a body (the frontend's static answer);
otherwise the addressed declaration plus every implementation. **A set of more
than one is resolved by trial** (`Walker.keepMostRoles`): each member is walked in
isolation, as though it were the only one, and the member whose walk reaches the
most distinct roles is kept -- ties to the earlier, which for a rescued set is the
nearest in the class hierarchy. The others leave no trace: their rows are dropped
and the bodies they entered are forgotten. Until 2026-09-26 a non-role member was
never entered, because walking all fourteen `executeSeleniumAction` bodies as
though each ran would attribute every call in them to the source role; walking
them one at a time and keeping one answers that without giving up a role that
sits *inside* an implementation rather than on it. The cost is the other side of
the same choice: two members that are both roles no longer both emit -- the one
reaching more roles stands for the call.

There is **no same-file branch and no cross-file branch**, because there is
nothing to distinguish. A `fullName` is a *global address* with the location
inside it, so one string join covers both:

```
[SAME FILE]   caller duffy/admin.py:20
  call.methodFullName  duffy/admin.py:<module>.AdminContext.__init__
  callee.fullName      duffy/admin.py:<module>.AdminContext.__init__      identical

[CROSS FILE]  caller duffy/admin.py:27
  call.methodFullName  duffy/database/__init__.py:<module>.init_model
  callee.fullName      duffy/database/__init__.py:<module>.init_model     identical
```

Python embeds the file path in the name; Java embeds the package
(`fr.enedis.chutney.…`). Crossing a file therefore costs nothing extra.

### How that address comes to exist

None of it is the walk's work. It happens when the CPG is built:

1. **the frontend writes an address.** Each `CALL` gets a `methodFullName` — its
   best guess at the target's fully qualified name, from imports, scope and
   whatever types it inferred.
2. **a later pass joins by string.** Every `METHOD` is indexed by `fullName`
   (unique: 4397 of 4397 on `CentOS_duffy`) and each call's address looked up.
   Found → an edge. Not found → an **external stub** is minted, named after the
   string, and the edge points at that.

Measured, that gives 100% of `Enedis-OSS_chutney`'s 10,533 stubs a `fullName`
identical to the call's address: a stub *is* the string, wrapped in a node.

### Three outcomes, and what the walk does with each

| `c.callee` gives | walk |
| --- | --- |
| an **internal** method — same file or another | **descends**: `walkBody` starts at that method's own `cfgFirst` |
| an **external stub** | no descent (there is no body), but the stub carries the *name*, so a library-cited role still matches through `externalRoles` |
| **nothing** | dead end |

```
                        no edge   external stub   internal (same/cross file)
CentOS_duffy   (py)      26.2%        63.7%            6.5% / 3.6%
freqtrade      (py)      15.1%        64.1%            9.3% / 11.5%
chutney      (java)       0.0%        72.9%            7.8% / 19.3%
```

So the walk descends on 10–27% of call sites. Java's cross-file edges outnumber
its same-file ones 2.5:1 — static types and explicit imports let the frontend
compute the address; Python's `ANY` types often do not.

### A secondary route the walk never sees

The string join is primary, not sole. `CentOS_duffy` has 3,210 calls whose
address is `<unknownFullName>` but only 1,807 with no edge — so ~1,400 were
linked despite having no usable name, by Python's type-recovery pass inferring
the receiver:

```
db_async_session.begin()   methodFullName=<unknownFullName>   callee=AsyncSession.begin
```

Java links polymorphically in the same spirit: the call names the interface and
the edges go to *every* implementation. `SeleniumAction.execute`'s
`executeSeleniumAction()` carries fourteen; the old single-edge read always took
`SeleniumQuitAction`'s, whichever subclass was actually running.

### When the frontend links only the interface: `dispatch`

That linking needs the interface's own signature. A **library** interface has
none in the graph -- JAVASRC reads the checkout, not its jars -- so a call on it
is left at `<unresolvedSignature>` with one external stub and no edge to any
implementation. On `cloudfoundry_uaa` every Enforcer path crossed one:

```
authenticationManager.authenticate(authentication)
  methodFullName = org.springframework.security.authentication.AuthenticationManager.authenticate:<unresolvedSignature>(1)
  callee         = that stub [external], no body
```

`CallGraph.targetsOf` covers exactly that case, behind the profile's
`dispatchesToOverrides` (on for Java, off for Python, whose receivers carry no
declared type). When no callee has a body, it asks `overridesOf` for the
repository methods of the same name and arity declared in any subtype of the
type the call is addressed to (`declaredOwner`: the fullName's owner, else the
receiver's type; a library parent is also matched by simple name). They go to
`keepMostRoles` like any linked dispatch set, and edges reached through them
are tagged `mechanism: "dispatch"` -- the call is in the code, *which* override
runs is Spring's choice at runtime, and a reader filtering on `call` still gets
the order a literal-call walk produces. Two bounds keep it a single inference
rather than a search: more than `MAX_OVERRIDES` (32) candidates follows none,
and inside a trail already entered by dispatch a second dispatch is not taken --
UAA's 18 `AuthenticationManager` overrides delegate to each other, and letting
each fan out again chained six of them into one `via` and quadrupled the query
time. Among alternatives reaching as many roles, the one through fewer helpers
wins.

### Framework routes: when Spring makes the call

`dispatch` needs a repository class implementing the interface the call names.
Spring Security's login hand-off has none:

```
StandardLoginRequestFilter.java:49   (hygieia_api -- the Enforcer)
  return this.getAuthenticationManager().authenticate(authRequest);
  methodFullName = <unresolvedNamespace>.authenticate:<unresolvedSignature>(1)
  receiver       = getAuthenticationManager()   type ANY   (inherited from a Spring class)
```

At runtime that manager is Spring's `ProviderManager`, which loops over the
registered `AuthenticationProvider`s and calls the one whose `supports()`
accepts the token -- here `StandardAuthenticationProvider.authenticate`,
registered at `WebSecurityConfig.java:127`. The repository implements
`AuthenticationProvider`, not `AuthenticationManager`, and the loop is library
code, so nothing in the graph joins the filter to the provider. The walk stopped
at line 49, and because the Enforcer reached no role the whole derivation came
back empty: the Verification manager's own direct calls to the Password store and
Comparator were never walked.

A **route** is the profile's declaration of such a hand-off, `Lang.routes`: *a
call to `method` on `owner` runs the first of these steps that resolves to
repository code*. A step is `anyOf` -- the framework picks one target at runtime,
so they compete in `keepMostRoles` like a dispatch set -- or `inOrder` -- the
framework runs each in turn, so they are walked as consecutive hops of the one
call. A target resolves to the repository's implementations of `type.method`
(`implementationsOf`, which is `overridesOf` without the call), else to the
repository's injection providers of the type (below). Java declares one route,
for `ProviderManager`; see `java.sc` § `routes`.

- **Last resort, and additive.** `targetsOf` tries a callee with a body, then
  overrides (`dispatch`), then a route, then providers. A route only fills a gap,
  and what the call resolved to without it -- the external stub, and any library
  role cited on the call site -- stays as the first hop, so no edge the walk
  emitted before can disappear.
- **The receiver's type may be unknown.** When a Spring superclass supplies the
  getter, JAVASRC leaves the call unresolved and the receiver `ANY` -- true of all
  three repositories this was built for. So a route also matches on the getter
  the receiver came from (`getAuthenticationManager`, `authenticationManager`,
  `authenticationManagerBean`); a typed receiver matches by owner name, full or
  simple.
- **The repository's own manager wins.** If the repository implements the
  route's owner type itself, that is ordinary dispatch and the route is not used.
- **A route is not a guess.** Its hops are marked `bridged`: they do not use up
  the trail's one dispatch hop -- on hygieia the provider's own
  `authService.authenticate` is a second, ordinary dispatch into
  `DefaultAuthenticationServiceImpl`, and blocking it lost the edge. Edges past a
  route are tagged `dispatch`, and `via` starts with the route's label,
  `[spring] ProviderManager.authenticate`, so a reader can see which hop is
  framework knowledge rather than code.
- **Entry follows.** `Entry.reachesFrom` reads the same `targetsOf`, so a filter
  that reaches a role through a route is a root like any other. Entry selection
  did not change.

**Injection providers.** A call on an interface the repository never implements
usually runs a library object the repository builds itself:

```java
@Bean public PasswordEncoder passwordEncoder() { return new BCryptPasswordEncoder(); }   // Hasher cited here
...
passwordEncoder.matches(raw, user.getPassword());                                         // in the Verification manager
```

`Lang.providerAnnotation` names the annotation (`@Bean`); `providersOf(T)` is
every method carrying it whose return type is `T` -- full name, else simple name.
When nothing else resolves the call, its providers' roles are reached **at the
call**: emitted together with its other targets and ordered with them by the
pattern (`pairsFor`), tagged `dependency-injection`, and never walked into -- a
provider's body builds the object at startup, it is not what the call does. The
first cut emitted providers as a hop of their own, *after* the call's other
targets; on `Open-Core-Finance_corefinance`, where one side cites the Hasher on the
`@Bean` and the other cites it on the `matches` call, that reversed Hasher and
Comparator on one side only and cost Level 3b a sixth for an ordering the code
does not contain. A role cited on the constructed library class rather than the
method is found through `Lang.constructorName` -- `new X()` is a call to
`X.<init>` -- and a library role whose call sites lie in a provider owns that
provider (see `Ownership`).

**What a route assumes.** That the framework behaves as declared: the manager is a
`ProviderManager`, the provider is registered with *that* manager, `supports()`
accepts the token. None of it is checked. On `cdkjframework_wiki-framework` the
filter's manager is the global one from `AuthenticationConfiguration`, and the
provider reaches it only because Spring Boot registers a sole
`AuthenticationProvider` bean there -- true of the repository, false the day an
application using it defines a second. A route states the framework's contract,
not one deployment's wiring; `dispatch` and the `via` label say so on every edge
it produces.

**Why these two.** A text survey of every Java role citation in the cloud
annotations (204 recovery results; 120 records with a checkout) grouped each by
the framework type its class implements or the annotation on it:

| hand-off | records | here |
| --- | --- | --- |
| `@Bean` provider of the injected type | 28 | providers |
| `AuthenticationManager` → a repository `AuthenticationProvider` | 13 | route, step 1 |
| `AuthenticationManager` → Spring's `DaoAuthenticationProvider` → repository `UserDetailsService` / `PasswordEncoder` | 9 | route, step 3 |
| security-config method cited as the Enforcer (`SecurityFilterChain` `@Bean`, `configure(HttpSecurity)`) | 26 | **not routed** -- setup code; its edges would be registrations at startup, not the request's order. A citation to fix |
| resource server: `jwtAuthenticationConverter(x)` → `x.convert` | 8 | not routed -- the pattern there is almost all Spring's; a route adds one setup-time edge |
| SpEL `@PreAuthorize("@bean.m()")` / Shiro realm | 3 / 2 | not routed |
| Spring Data repository interface | ~18 | nothing needed: the cited interface owns its declared methods, and calls on it land there |

**Measured 2026-09-29**, a Java batch rescored against reference mappings
(15 records, 13 scored):

| record | before | after |
| --- | --- | --- |
| `hygieia_api` (pwd) | no interaction either side | both sides: `Enforcer → Verification manager` (route, via `StandardAuthenticationProvider.authenticate`), `→ Password store`, `→ Comparator`; 3a 1.0, 3b 1.0 |
| `cdkjframework_wiki-framework` (pwd) | none | GT 3, RR 5, 3 shared; 3a 0.6, 3b 1.0 |
| `indiana-university_lms-embedded-services` (token) | GT 1, RR 0; 3a 0.0 | GT 2, RR 3, 1 shared; 3a 0.25 |
| `Open-Core-Finance_corefinance` (pwd) | 3a 0.75 | GT's Hasher (`BCryptPasswordEncoder` in a `@Bean`) reached from `passwordEncoder.matches`; 3a 1.0, 3b 1.0 |
| the other nine | | identical interactions |

Batch Level 3a 0.635 over 8 records → 0.718 over 10; 3b 1.0 over 4 → 1.0 over 6.
PYTHONSRC is byte-identical before and after on `bluesky_tiled`, `CentOS_duffy`,
`freqtrade` and `ArionXiv`: `python.sc` declares no route, provider annotation or
constructor name, and the three members default to empty.

### The one rescue: the file's own import

An address can be **wrong rather than missing**, and that case is recoverable
because the source states the answer. JAVASRC attributes a statically imported
call to *the class it appears in*:

```java
import static ...validation.ActionValidatorsUtils.notBlankStringValidation;   // line 11
...
notBlankStringValidation(queueName, "queueName");                            // line 65
```

```
address written   fr.enedis.chutney.action.amqp.AmqpBasicConsumeAction.notBlankStringValidation:<unresolvedSignature>(2)
                  └─ the class it appears in ─────────────────────────┘        WRONG owner
real method       fr.enedis.chutney.action.spi.validation.ActionValidatorsUtils.notBlankStringValidation:<unresolvedSignature>(2)
                  └─ the imported class ──────────────────────────────────────┘   ActionValidatorsUtils.java:56
```

The signature suffix is **identical on both sides** — only the owner is wrong, so
the join misses and a stub is minted for a method that is in the repository.

`rescuedCallees` reads the import the frontend ignored. For a call that reached
only library stubs it asks the profile `Lang.importedCallees(c)`: which
repository methods does this file's import say the call runs? Two things in that
answer are the language's, so both live in the profile:

- **is the call to a bare name?** Only `f()` is named by an import; `x.f()` runs
  whatever `x` is. PYTHONSRC gives `f()` the receiver `f` itself and `x.f()` the
  field access `x.f`; JAVASRC writes `f()` as `this.f()`. So the profile checks --
  and Python answers `x.f()` too, when the frontend typed `x` only by an
  annotation's bare name (see `python.sc` § `importedCallees`).
- **where does the import point?** The profile's own `importedDefinitions` -- the
  same answer the locator uses for a citation: a fully qualified name in Java,
  the module-path chain through `__init__.py` re-exports in Python.

**An overloaded member is told apart by arity** (arguments vs parameters,
receiver excluded) when that leaves any; several left go to the walker as a
dispatch set, resolved by trial like any other. Arity is a hint, not a filter,
because a keyword argument has index -1 in PYTHONSRC: `svc.create(name=…)`
counts no arguments against seven parameters. The Java rescue used to split the import at its last dot
(`importedTarget`) and take the first declared overload -- 12 of chutney's 323
rescues, e.g. `findAvailableTcpPorts(num)` against `(int)` and `(int,int,int)`.

**No guessing.** The import is an explicit statement in that file that this name
means exactly that entity. This is not the repository-wide name search that
**What guessing cost** records; it is scoped to one declared binding.

**Python, the package case.** PYTHONSRC reads `from aquiles.configs import
load_aquiles_config` as the file `aquiles/configs.py`. There is no such file: the
module is the package `aquiles/configs/__init__.py`, which re-exports the
function from `aquiles/configs/configs.py:121`.

```
address written   aquiles/configs.py:<module>.load_aquiles_config           a file that does not exist
real method       aquiles/configs/configs.py:<module>.load_aquiles_config   via configs/__init__.py
```

`python.sc` tries both spellings of a module (`a/b.py` and `a/b/__init__.py`),
follows re-exports to the defining file, and matches a `src/` layout -- but only
below a *source root*, a directory that is not itself a package, so
`from jwt import encode` can never land on the repository's own
`app/utils/jwt.py`. Python was switched off here before, because the only
resolver was that single one-file guess; the chain replaced it.

Measured 2026-09-24 (calls that reached only library stubs → rescued):

| repo | stub-only calls | rescued |
| --- | --- | --- |
| `Enedis-OSS_chutney` (java) | 10533 | **318** (the old split-at-the-dot rescue: 323, 12 of them an arbitrary overload) |
| `KaiserpfalzEDV_kp-commons` (java) | 858 | 0 |
| `CentOS_duffy` (py) | 4394 | **14** |
| `freqtrade_freqtrade` (py) | 30646 | **1327** |
| `Aquiles-ai_Aquiles-RAG` (py) | 1833 | **42** |
| `djpugh_fastapi_aad_auth` (py) | 1266 | **19** |

A random sample from each repo was checked by hand; every one lands on the
function the file imports. On derived interactions, the stored Aquiles recovery
(`authentication_pwd`) went from **0 to 4**:

```
Enforcer → Verification manager       main.py:310     authenticate_user(...)   via aquiles/auth/__init__.py
Enforcer → Comparator                 main.py:310
Verification manager → Password store jwt_aq.py:28    load_aquiles_config()    via aquiles/configs/__init__.py
Comparator → Password store           jwt_aq.py:28
```

and the Java case, with `Enforcer` on `validateInputs` and `Verifier` on the
statically imported validator, still derives
`Enforcer → Verifier  AmqpBasicConsumeAction.java:65  call`.

### What is still lost here

Two shapes, neither fixable from the annotation side (a third, Python's package read as a module, is now rescued above):

| | |
| --- | --- |
| **Java: a method called through an interface** | `getMetadata`, `getNameSpace` on `kp-commons` — declared on an interface and called through it. No import names them, so there is nothing to read; resolving it needs the receiver's declared type |
| **Java: generated code** | Lombok's `$fillValuesFrom`, `AvatarOptions$AvatarOptionsBuilder` — never in the source, so no parsed method has the name |


---


## 5. Descending

*The two `walkBody` calls inside `emitAndDescend`; termination from
`Walker.enteredBodies`, `visitedNodes` and `Walker.MAX_DEPTH`.*

A call that reaches a role **enters that method** before the walk carries on, so
anything found inside appears *between* the outer call's edge and the next one
— the way a sequence diagram nests:

```
show_tenant
 ├─ @63  EMIT Enforcer → Resolver
 │       └─ inside get_tenant_id: sync_session_maker, execute … no roles, nothing emitted
 └─ @63  EMIT Enforcer → Proxy
```

A call landing on something that is **not** a role is entered anyway, emitting
nothing, still attributed to the caller's roles, with the helper appended to
`via`. Roles are architectural and are normally separated by ordinary helpers,
so getting through `_dispatch` to the next role is the point. `via` is the trail
since the last role — the only thing distinguishing a direct call from one found
six helpers away.

There is no hop budget. Termination comes from bookkeeping: a
`(method, start node, source roles)` triple is entered once (`enteredBodies`),
and a node is visited once per body (`visitedNodes`), so loops end. `MAX_DEPTH`
(512) is a JVM-stack guard, not a search limit.

---

## 6. Dedupe and rank

*`── 5. emit` — `firstOccurrences`, then the
`interactions` array.*

The walk emits one row per *reaching event*; the token `A → B` names roles only.
Many events collapse onto one token — several call sites, several methods of a
cited class, both arms of a branch, two paths through different helpers, a pair
that is both wired and called. Duplicates collapse to their **first**
occurrence, and the surviving rows are ranked `1..n`.

Which occurrence survives is the point. Every consumer keys on the token with a
dict (`rank_a` in `_kendall_tau`, `gt_rank` and `gt_evidence` in `_rows`), and a
dict comprehension over a list keeps the *last*. Nothing would crash without the
dedupe and no Level 3a count would move — coverage is set-based — but a pair
first reached at rank 1 and again at rank 40 would score as rank 40, and tau
would count every pair involving it as a late interaction. Collapsing here forces
first-occurrence, which is the only reading consistent with a traversal that is
an execution.

The cost: dropped rows take their `file`, `line`, `via` and `mechanism` with
them. Where a pair is both wired and called the wiring row wins, since entry
edges are emitted first — so filtering the output to `mechanism == "call"`
under-counts pairs that are genuinely both.

**Result:** `{schemaVersion, lang, entryRole, entryRule, anchoredRoles[],
interactions[]}`. `detector.py` refuses a `schemaVersion` it does not read, so
changing this shape means bumping `SCHEMA_VERSION` there. Version 2 added each
interaction's `path` and `parent`.

---

## Why CFG order and not line numbers

Line numbers do not run in order. `get_tenant_id` spans lines 53-57 and the
traversal runs **54 → 55 → 56 → 55 → 54**: a nested call descends into a later
line and returns to an earlier one partway through, and a `with` block's
`__exit__` runs after the body. So "stop when the line changes" would halt
before anything useful ran.

**The line is a label carried on a node, not a unit of anything.** It is read at
emission time so a row can say where it happened, and never used to decide what
runs next. It is also why `firstCfgNodeAt` *steps* the CFG to find a line's first
node rather than filtering by line: first-in-the-list is not first-to-run.

---

## The three files

| | |
| --- | --- |
| `walk.sc` | the whole derivation, language-independent |
| `python.sc` / `java.sc` | one `LangSpec` profile |
| `detector.py` | posts `walk.sc` + one profile as a **single script** |

The seam is narrow on purpose: a profile answers questions about its own
frontend — where a declaration's line sits, how containment is spelled, how the
ties it makes are ranked, how an import maps to a file, what it discarded — and
the walk owns every decision that needs the graph, so there is one walk rather
than one per language.

A member earns a place on the seam only if **two profiles could legitimately
answer differently**. Anything every profile would answer identically is a CPG
fact, not a language fact, and stays in the walk. `walk.sc` therefore names no
language, no framework and no library anywhere in it — that is checkable, and
worth re-checking after an edit:

```
grep -iE 'python|pythonsrc|javasrc|java\.sc|fastapi|spring|bcrypt|\.py:' joern/walk.sc
```

(It should print nothing. Bare `java` is not in the pattern because `walk.sc`
imports `java.nio.file` to write its result — the JVM's standard library, not a
language profile.)

Sections of `walk.sc`, in file order, which is also the order they run:

| § | | narrative |
| --- | --- | --- |
| 1 | call resolution (`calleeOf`) | §4 |
| 2 | locate roles | §1 |
| 3 | the walk (`startsOf`, `walkBody`) | §2, §3, §4, §5 |
| 4 | entry role, then the single `walkBody` call | §2 |
| 5 | emit | §6 |

Sections 1-3 only define things; nothing executes until §4.

**Two properties hold by construction.** *Branch-insensitive* — an interaction
`A → B` exists iff some call path runs from a method of A to a method of B
passing through no method of a third role; whether that path is conditional,
looped or dead is not modelled. *Ordering is graph position* — the order rows are
emitted in **is** the order the CFG was stepped in.

Reasoning per definition is in **Implementation notes** below, under the
definition's own name; the code carries only a one- or two-line docstring.

---

## Known issues

| | |
| --- | --- |
| **a third of calls carry no target, so the call graph is fragmented** | on 30–44% of call nodes the frontend records `<unknownFullName>` and no callee edge (JAVASRC 29–31%, PYTHONSRC 34–44%). **The walk itself is unbounded** — there is no hop budget — but it runs out of graph almost at once: from the busiest method of a 7850-method repo only **39** methods are reachable at any depth. So two roles not within about three *resolved* calls of each other are not connected here however far the walk is allowed to go. This is the ceiling of the approach, and **chasing it means guessing** — see below |
| **nothing separates derived edges from inferred ones at scoring time** | `mechanism` records whether an edge was a literal call or framework wiring; Level 3 scores them alike. Reporting Level 3 over `mechanism == "call"` only would say how much of a score rests on inference — but see the dedupe caveat in §6 |
| a role the entry never reaches contributes nothing | the walk starts once, so an unconnected role is absent from the order rather than given a walk of its own. A true statement about the call graph, but a mapping can be good and still derive little. Most often the break is a framework hand-off; the Spring Security login one and `@Bean` injection are routed (§4, **Framework routes**) |
| framework hand-offs outside the declared routes still break the walk | Spring's resource server (`jwtAuthenticationConverter(x)` → `x.convert`), SpEL `@PreAuthorize("@bean.m()")`, Shiro `Subject.login` → a `Realm`, WebFlux's `ReactiveAuthenticationManager`. Each is one `Route` in `java.sc` once it is worth one; the survey behind the current two is in **Framework routes** |
| a route is the framework's contract, unchecked against the repository's wiring | which provider a `ProviderManager` holds, and whether `supports()` accepts the token, is decided at runtime; the route assumes the declared behaviour. Edges past one are `dispatch` with the route's label in `via` |
| `methodsOfType` is built for every class | and read for the handful that are cited; it duplicates what `methodsByOwner` derives in one pass |
| the differential against the deleted Python walk was never re-run | the one comparison made covered 8 of 30 rows |

**Reachability, measured.** Breadth-first from the highest-fan-out method:

| repo | internal methods | newly reached per depth | total reachable |
| --- | --- | --- | --- |
| `freqtrade_freqtrade` | 7850 | 21 → 13 → 4 → **0** | **39** |
| `Enedis-OSS_chutney` (Java) | 5145 | 18 → **0** | **19** |
| `CentOS_duffy` | 1443 | 3 → 5 → 1 → **0** | **10** |
| `Aquiles-ai_Aquiles-RAG` | 401 | 4 → **0** | **5** |

The depth column, not a limit: the walk follows every resolved call as far as it
goes. A `maxHops` budget existed until 2026-09-22; it was **removed**, and
removing it changed no result on any case tested — because the graph runs out
long before a budget would bite. The only remaining bound is `MAX_DEPTH` (512),
a JVM-stack guard set far above anything real code reaches.

**What guessing cost.** The script used to ask, at every call, whether each plain
`IDENTIFIER` argument was a function — answering by searching the repository for
that name. On `CentOS_duffy` it derived six interactions of which **five were
wrong**: a comprehension lowered to `tmp0.append(...)` matched 256 unrelated
bindings of `tmp0`, and following them relocated the walk into
`retire_unretire_tenant`, where two *real* calls were then emitted under the
wrong source role. **A bad resolution does not add one bad row; it relocates the
walk and mislabels everything downstream.** With the guess removed the same input
gives the two rows at the top of this document. This is why every event in §4
follows an edge the frontend resolved, and why only a `METHOD_REF` counts as a
callable handed over — that node exists *because* the frontend resolved the name.

## Validation so far

Measured 2026-09-22 against a live service (Joern 4.0.600, CPGs built by
`joern-parse`), on `CentOS_duffy`, `freqtrade_freqtrade`, `Aquiles-ai_Aquiles-RAG`,
`djpugh_fastapi_aad_auth` (PYTHONSRC) and `Enedis-OSS_chutney`,
`KaiserpfalzEDV_kp-commons` (JAVASRC).

| | |
| --- | --- |
| stripped-path call-resolution fallback | fired **0** times across ~73k call nodes |
| call sites reaching a repo method, indexes vs `c.callee` | 534/695, 9056/10017, 161/188, 3853/3912 — the edge strictly dominates |
| `METHOD_REF` → `referencedMethod` | resolved 6379 of 6379 on freqtrade, agreeing with the name index exactly |

An earlier comparison against the deleted Python walk: 30 rows attempted, 14 lost to
Cloudflare 524, 8 both-empty, 8 actually compared (6 gains, 2 losses).

---


# Implementation notes

One section per definition in `walk.sc`, named after it. The code carries a
brief docstring; the reasoning lives here so the query stays readable.


## `LangSpec`

Everything that differs between one frontend's CPG and another's.

The core members are **pure functions of strings and numbers** -- where a
declaration's line sits, how an import maps to a path, how the frontend spells
containment, how it ranks the ties it makes, which methods are module or class
bodies (`isModuleOrClassBody`, read off the method's name). `entryEdges` is
allowed the graph, because the thing it owns genuinely needs it.

Three members are **data with an empty default** -- `routes`, `providerAnnotation`,
`constructorName`: what a framework does with a call, which annotation marks an
injection provider, what the frontend names a constructor. A profile with nothing
to say inherits the default, which is why `python.sc` did not change when Java
gained them. The profile only declares; `targetsOf`, `emitAndDescend` and
`Ownership` do everything that needs the graph, so the rule below still holds.
`Route` names no framework either -- the Spring one lives in `java.sc`.

**What decides whether something belongs here.** Not "is it about a language" --
that lets anything in. The test is whether **two profiles could legitimately
answer differently**. A member every profile would implement identically is a
fact about the CPG, not about a language, and belongs in the walk: that is why
`<operator>.*` (the filter separating a call the programmer wrote from one the
frontend synthesised) and the placeholder filenames `rel` discards stay there,
behind `isRealCall`, with a comment saying so. The same test took `isSynthetic`
off the seam in 0.14.0 -- both profiles answered `name contains '<' or '>'`, which
is Joern's naming convention, not a frontend's -- and `nestedDefOperator` with it,
once the nested-definition rule stopped depending on how one frontend lowers a
`def` (see `isCallArgument`).

**The trait names no language, and no framework.** A reader of `walk.sc` should
not be able to tell which profiles exist. Each member states the question and
no answer; the reasoning for an answer lives in the profile that gives it and in
**Language profiles** below. The walk's own comments were genericised to match
-- where they used to cite `CentOS_duffy`'s `Depends()` or FastAPI by name, they
now state the rule and point here for the measurement. The rule the split holds
to is unchanged: **only the walk decides what an interaction is.**

The rule the split holds to is not "a profile may not touch `cpg`". It is
**only the walk decides what an interaction is**. A profile answers questions
about its own language; the walk turns answers into edges, in one place, the
same way for every frontend. That restriction is the whole discipline of the split: the walk below
owns every decision that needs the graph, and a profile owns only what can be
decided without it. A profile is therefore a page of code readable in one
sitting, and the walk stays one implementation instead of growing a `if
(language == ...)` at each frontend quirk. A profile is supplied by
concatenation, not by import: `detector.py` sends this file followed by
`python.sc` or `java.sc`, and the second defines `object Lang extends LangSpec`.
The service runs whatever single script it is posted, so one file arrives on the
wire and the pair is chosen here, in the client.


## `externalRoles`

Library-cited roles, keyed by **(citing file, name)**. A library entity has no
node here, so it is matched by callee name -- but the name alone is not an
identity: two roles citing `hashpw` in different files are different claims, and
the citing file is the only locality such a match has.

**A role that names nothing is excluded from it.** An entity-less citation has an
empty name, and the reaches that carry no name of their own -- `data`,
`dependency-injection`, `route-registration` -- look up `(file, "")`. Keyed on the
empty string, such a role matched every one of those in its file: measured on
`CentOS_duffy`, a `System` row citing only `tenant.py:40-44` collected two edges
it had no relation to. The empty key can never match a real call, which always
has a name, so it only ever fired wrongly.

Level 3 also refuses such a row upstream (`agreed_roles` requires an entity on
both sides), but the guard belongs here too: callers outside Level 3, such as
`secpat recover` stage 3, send raw mappings that Level 3's filter never sees.


## `appearance`

Role -> the earliest step of the pattern it appears in, speaking or spoken to.
The ordering authority for roles reached *together*. Several roles on one entity
are reached by a single call instruction, so they occur at one instant and no
call-site line or CFG rank can separate them. The pattern's own step numbering
is the only ordering both sides of a comparison can agree on -- and both sides
are derived against the same reference pattern, so ordering by it makes them
agree by construction on something the code leaves arbitrary, instead of
disagreeing arbitrarily and charging Kendall tau for it. Reads an explicit
`step` when the entry carries one, falling back to the entry's position. A role
the pattern never names sorts last, stably.


## `pairsFor`

Which (source, target) pairs an edge between two entities really stands for: the
cross product, self-pairs dropped, **all of them emitted**, ordered by
`appearance`.

It used to filter -- when more than one pair survived, it kept only those the
pattern prescribed. That was wrong on the semantics of a co-located citation.
Three roles on `pwd_context.verify` is a claim that the one call *is* all three,
not a claim that one of them is right and the others are noise, so selecting
among them discarded a true interaction. Measured on `CentOS_duffy` with
`Hasher` and `Validator` both cited on `bcrypt.checkpw`: filtering emitted only
`Enforcer -> Validator`, because the pattern names that step and not
`Enforcer -> Hasher`. Order-only emits both, in the pattern's own step order.

It also makes the claim about the pattern literally true rather than nearly
true. The pattern now only ever *orders* -- it selects nothing, anywhere -- so
the derived order cannot agree with the reference by construction.

The set of prescribed `(from, to)` pairs that did the filtering is gone with it;
only `appearance` is read from the walkthrough now.


## `calleeOf`

Now `calleesOf`: every method a call may run -- the CPG's own call edges, read
whole, plus one rescue when none of them is in the repository (`rescuedCallees`).
The occurrence reading of a citation (`occurring`) goes through it too, taking the
member the call's address names, so a citation on `compareExecutor.compare(...)`
locates `CompareExecutor.compare` rather than the first implementation stored. **Locating the method to continue
in** above is the whole story — how the address is built and joined, the three
outcomes, the static-import rescue, and what is still lost. **Known issues**
carries the measurement that removed the two name indexes this replaced.


## `entryEdges`

Methods invoked on entry to a body that no CFG node records -- framework wiring
the frontend discarded. Returns (target, evidence line, mechanism), and
`walkBody` emits them before stepping the first node, because a parameter is
bound before the body runs.

**The whole of dependency injection lives behind this member**, in the profile.
It is one language's problem: PYTHONSRC drops a parameter's default expression,
so `Depends(get_config)` has to be read back out of the source text and resolved
by name against the graph. `python.sc` therefore holds the file reading, the
signature join, the default-expression regex, `methodForName`'s three shapes and
the six lazy indexes behind it -- about 190 lines. `java.sc` returns `Nil` in
one, and a Java query builds, reads and parses none of it.

It used to sit in `walk.sc`, gated by a `hasParameterDefaults` boolean. That made
the *behaviour* right and the *structure* wrong: Python-shaped source parsing and
six Python-shaped indexes sat in the language-independent file, inert only
because one constant happened to be false, and a second language with defaults
would have landed there rather than in its own profile. Moving it took `walk.sc`
from 711 lines to 531 and left it with no source reading and no name resolution
at all.


## `isCallArgument` (and the `METHOD_REF` case in `walkBody`)

A `METHOD_REF` is how both frontends lower a callable written in a body: a Java
lambda (pointing at a generated `<lambda>N` method), a Java method reference
(`this::handle`), a Python nested `def` (`inner = <METHOD_REF>`), a Python
`lambda`. When it sits inside a real call's arguments, `methodsPassedTo` enters
it at that call (`route-registration`), and `isCallArgument` says so. Anywhere
else -- returned, assigned, in a ternary -- the body is handing the callable on,
and `walkBody` enters it where the CFG reaches the node, as `nested-definition`.
`referencedMethodInRepo` keeps only targets in this repository.

**Why it exists.** Until 0.14.0 only call arguments and Python's
`<operator>.assignment` were read, through a profile member naming that operator
(`nestedDefOperator`, empty for Java). A Java factory returning its filter as a
lambda was therefore a dead end. On `spring-cloud_spring-cloud-gateway` both
sides cite the Enforcer on `RequestRateLimiterGatewayFilterFactory.apply`, whose
body ends in `return (exchange, chain) -> …`; that METHOD_REF is a child of the
`RETURN`, so the walk fell through to `case _` and the whole order came back
empty, Level 3 n/a with all four roles agreed. Now the walk goes `apply` →
`<lambda>0` → (passed to `flatMap`) `<lambda>1` → `limiter.isAllowed(routeId, key)`,
whose dispatch set the frontend already linked, and derives `Enforcer → Limiter`
at line 132.

**Why the call-argument guard.** The CFG reaches an argument's METHOD_REF before
the call receiving it, so without the guard `foo(lambda, bar())` would enter the
lambda ahead of `bar()`, not at `foo` as before. With it, every argument lambda
keeps its path and its rank.

**The module/class-body guard** (`Lang.isModuleOrClassBody`). A Python module or
class body binds every `def` it contains as `name = <METHOD_REF>` without running
any of them; entering them would treat each function the file defines as
something the walk ran. Measured before 0.14.0, when the guard was
`isSynthetic(m.name)`, on `duffy/database/__init__.py` with a role cited on the
module-level binding at line 25, which starts the walk inside `<module>`: with
the guard **0** interactions; without it a fabricated `Enforcer -> Validator` at
`__init__.py:53`, whose only basis is that line 53 declares a function. The guard
no longer reads `isSynthetic`, which also matched `<lambda>N` and so would have
blocked a lambda returned from inside another lambda. If module code really runs
one of its functions there is a call for it, followed like any other, so the
guard drops only edges no call evidences. Java has no such body: a class body is
a TYPE_DECL, and `java.sc` answers false.

**A guard that was dropped.** `nestedDefsIn` also kept only targets lexically
nested in the walked body (`t.fullName.startsWith(m.fullName + ".")`), skipping an
alias like `handler = login`. The rule is now the same for both languages: a
callable handed on is entered whether it was defined there or elsewhere, as Java's
`Handler h = service::process` must be. No measured case motivated the old test,
and none of 25 scored Python records changed without it.

**What it claims.** Entering the callable where it is *built* is not where it
*runs* -- the gateway invokes the filter per request, FastAPI the closure per
request -- so the edge is framework wiring, like `dependency-injection` and
`route-registration`, and carries its own `mechanism` to stay separable. It is the
same claim the walk already makes for a lambda passed to a call.

**Ordering.** The callable's interactions are emitted where it is built, ahead of
anything the body does after it: exact for a returned lambda, which is the last
thing its body does; off by the statements in between for one assigned and called
later. JAVASRC gives the METHOD_REF no line, so the edge takes its parent's (the
`RETURN` on line 119 above).

**Measured 2026-10-05** (0.13.0 → 0.14.0, one model's batches re-scored): Java,
of 25 records scored under both, one changed -- gateway, Level 3a n/a → 0.667,
3b n/a → 1.0 (GT 3, RR 2, both shared); the 20 newly scored records route through
argument lambdas only. Python, 25 of 25 unchanged.

**Not covered: anonymous and local classes.** `return new GatewayFilter() { … }`
is an `<init>` call on a generated type, not a METHOD_REF; the walk reaches the
constructor and stops. 30 such sites outside tests in gateway alone.


## `emitAndDescend`

**Where an interaction is detected and written**, and the only place `edges` is
appended to. Called at every arrival — a call, a callable handed to a call, a
nested definition, an identifier's `REF`, an entry binding — with a *candidate*
and nothing more: the caller knows it arrived somewhere and by what mechanism,
not whether that is an interaction.

Three steps: whose code was arrived at (`roleOfNode`, or `externalRoles` by
name); is that a *different* role than the one being walked (`pairsFor`, which
drops self-pairs); and if so, append the row and continue inside the new role.
Most calls write nothing — every ordinary helper call in every walked body comes
through here and lands on the `targets.isEmpty` branch.

**A dispatch set is resolved by trial** (`keepMostRoles`). When a call may run
several implementations, each is walked alone and only the one reaching the most
roles keeps its rows, ties going to the fewest helpers in `via` and then to the
earlier -- see **Locating the method to continue in**. Walking every
implementation as though it ran would attribute all of their calls to `sources`;
walking each alone and keeping one does not.

**A call may resolve to several hops** (`Resolution.hops`), walked in order: the
call's own callees, then what a framework route runs next (**Framework routes**).
Within a hop the alternatives compete as above. Two parameters carry what a hop
knows: `bridged` -- the hop came from a route, so it does not set `dispatched`
(the trail's one guess is untouched) and edges past it are tagged `dispatch`
through the walk's `framework` flag -- and `provided`, the injection providers of
the object the call is made on. Their roles join the call's own targets *before*
`pairsFor` orders them, so a role reached by injection sits where the pattern puts
it among roles reached by the same call; they are tagged `dependency-injection`
and are never walked into, and descent into the call's target carries only the
roles that were not injected.

It was called `reach` until the name was found to obscure exactly this: it reads
as navigation, and the emission is the point. The role-level vocabulary it was
named after (`reachesFrom`, `reachedByOthers`, "a role the entry never reaches")
is unchanged — that is about roles reaching roles, which is a different question
from where a row gets written.


## `walkBody`

Execute one method body, node by node, starting at ``from`` when a citation
named a statement and at ``cfgFirst`` when it named a declaration (see
`startsOf`). ``sources`` is every role this body stands for -- an entity can be
several roles at once, and carrying the whole set keeps that ambiguity alive
until an edge is emitted, where `pairsFor` resolves it.

It takes no hop budget: a non-role callee is entered unconditionally and the
traversal runs until the graph runs out. Termination comes from
``enteredBodies``, not from a limit -- see §5 of the narrative above.


## `startsOf`

Where a role's walk begins. One rule in two shapes -- **start at whatever the
cited thing runs next**:

| cited | runs next | start node |
| --- | --- | --- |
| a declaration (METHOD, TYPE_DECL) | its **content** | `cfgFirst` |
| a statement (a role on a variable) | **that line** | the line's first node in the enclosing body |

Naming a function is naming its body, so a declaration continues at the body's
first node. Naming a variable is naming one statement, so it continues at that
statement -- and then keeps going, because the line after it really is what
happens next. Neither shape needs a stop rule.

The statement case is what makes `auth_scheme = FastApiKeycloack(...)` work. The
constructor call on that line is an ordinary CALL node the walk arrives at, so
`__init__` is entered by the same `calleeOf` path as any other call. Nothing
about constructors is modelled; the code just runs.

**What it replaced.** Two earlier designs handled the variable case specially.
`walkDefinition` was a second entry path that ran after the main loop and did its
own walking. `definingCalls` moved that into role location -- finding the
assignment, collecting its right-hand side, resolving each call by hand, and
appending the result to the role's node list as a second anchor. Starting the
walk at the line gets the same edges by *running* the code, in execution order
rather than in the order an AST collection happened to yield them, and leaves the
walk with one entry shape instead of two.

Measured on `CentOS_duffy`, roles `Guard` (`req_tenant`, `duffy/app/auth.py:45`,
a module-level binding) and `Factory` (`_req_tenant_factory`, same file line 11):
`definingCalls` derived **0** interactions, line-anchored entry derives **1** --
`Guard → Factory` at `auth.py:45`, mechanism `call`.


## `firstCfgNodeAt`

The first node on a line *in execution order*, found by stepping the CFG rather
than filtering nodes by line number. Lines are not contiguous in a CFG and not
monotonic along it: on `duffy/admin.py:53-57` the traversal runs 54 → 55 → 56 →
55 → 54, because a nested call descends into a later line and returns to an
earlier one partway through. So "the first node with this line" in list order is
not "the first one to run". `METHOD_RETURN` is skipped, because a method's own
declaration line carries that node and nothing else -- starting there would run
nothing.


## `anchorsAt`

Whether a citation recorded at `cited` names an entity spanning `[line,
lineEnd]`, where `lineEnd` is -1 for a node with no span. The most consequential
difference between frontends, and the reason this trait exists. An annotation
records the line a reader sees the declaration on; the CPG records the line the
frontend decided the node begins at, and those coincide in some languages and
not others.


## `jstr`

A JSON field as a string, tolerating `null` and a non-string type. Real role
mappings carry `"entity": null` for a role the recovery did not ground, and
`ujson`'s `.str` throws on null rather than returning empty -- which crashed the
whole query on any annotation with a missing role.


## `Locator` (`── 2. locate each role's entity in the graph`)

A citation is text -- a name, a file, a line -- so it has to be looked up. That
is the whole of this step: **find the node the annotation names.** It does NOT
convert that node into something else. An earlier version accepted only METHOD
nodes, so a role cited on a variable fell through to a containment fallback and
silently became `<module>` -- the wrong node, which then claimed the whole file
and fabricated an interaction. Every entity a role can be cited on already *is*
a node, so the lookup returns whatever kind it finds and the walk (§3) takes it
from there.


## `ts`

No span: TYPE_DECL carries no `lineNumberEnd` in Joern 4.0.627, only METHOD
does. So a span-based `Lang.anchors` falls back to equality on a class citation
-- which leaves one Java case unhandled, a citation on the `class` line of an
annotated type, since JAVASRC puts that line on the annotation too. Methods,
which is where the Spring annotations overwhelmingly are, do have a span and are
handled.


## `ps`

A parameter is a declaration like any other, and a role is regularly grounded on
one: `async def get_user_by_api_key(db: AsyncSession, ...)` has the session
handed *to* the function, so the Token manager role lives on `db` and the
interaction is `Validator -> Token manager`. Without this the citation matched
no node at all and the role was reported external. Nothing else needs changing
for it to be reached: `Identifier._refOut` follows a use of `db` in the body
back to this node, and the walk already emits a `data` edge for a use whose
declaration carries a role. And like LOCAL and MEMBER it has no body, so
`bodiesOf` returns nothing and a role on a parameter is only ever reached, never
walked from.


## `pool`

**Declarations are looked up in the cited file only.** It used to fall back to
every declaration of that name in the repository when the cited file had none,
and then apply the cited *line* to candidates in other files -- a line number
means nothing outside its file. Measured 2026-09-24: of the call sites naming a
method declared in another file, 29 (duffy), 182 (freqtrade), 308 (chutney) had
some *third* file declaring the same name at the same line, so `declaration-at-line`
would have claimed it. And when the cited file is not in the graph at all, a
unique name elsewhere was taken outright: chutney's p1 `Subject` is cited as
`login` in `login.service.ts` -- TypeScript, absent from the JAVASRC graph -- and
was anchored on a `login` parameter of `SoapFunction.java`.

The legitimate cross-file case is a citation on a *use* of something declared
elsewhere, and `occurring` handles that through the graph's own edge. Everything
else now reports the role unlocated -- the chutney p1 Subject is outside what this
graph can see -- and an unlocated role is read as a library call, whatever its
`status` says. (Until 2026-10-03 a `present` row that located nothing raised
`AnnotationGraphMismatch`; it was removed because a recovery's statuses cannot be
corrected after the run, and whether a `present` row is really a library call is a
status disagreement that Level 1 already scores.) A citation with no `file` still
searches the repository.

**When the cited file only imports the name**, the import is followed to the
file that defines it (`viaImport`, tried after `occurring`). How an import is
followed is the language's business, so it sits behind two `LangSpec` members:

| member | asks | Python | Java |
| --- | --- | --- | --- |
| `importedDefinitions(file, name)` | where is the name this file imports defined? | module path → files (`moduleFiles`, then a `src/` tail match), hop by hop through `__init__.py` re-exports, to a module-level `def`/`class`/assignment | fully qualified name, in Java's order: single import, own package, on-demand (`a.b.*`, `import static X.*`) |
| `isImportBinding(local)` | is this LOCAL an import, not a definition? | yes when assigned by an import call (PYTHONSRC lowers `from m import f` to `f = <operator>.import(...)`; older builds named the call `import`, and both are accepted), and as a closure capture in a nested scope | never: JAVASRC binds no LOCAL for an import |

The second matters as much as the first. PYTHONSRC binds every imported name as a
LOCAL assigned by an `import` call, so before this a citation in `aquiles/main.py` on
`get_current_user` anchored on that import (no body, nothing to walk) instead of
the `def` at `aquiles/auth/jwt_aq.py:50`, two hops away through
`aquiles/auth/__init__.py`. Import bindings are excluded from the cited file's
candidates and from occurrences. They are recognised from the node itself -- a
LOCAL assigned by `<operator>.import(...)` -- not from a table of import lines: a table
keyed on (file, name) held one line, so a file importing `run_mcp_serve` inside
two functions (`aquiles/cli/cli.py:109` and `129`) had its first import taken
for a definition, and every citation of the name anchored on that import
instead of `aquiles/utils/utils.py:446`. The cited line belongs to the importing file, so
across the import only a single best candidate is accepted.

The import is tried **only when neither reading of the line found anything**, and
before any same-named declaration elsewhere in the file, which says nothing about
scope. The case: a name inside a multi-line import, whose line the graph does not
record (`amount_to_precision` at `exchange.py:88`, recorded at 82), in a file where
a method of that name also exists. An import that leads back to the cited file's
own declaration is dropped from this reading -- Java's same-package rule finds the
file's own class -- so an annotated class cited in its own file is labelled by the
in-file candidates, not as `import`.

**A citation naming nothing locates nothing.** A bare line range, or a generated
name (`<module>`), matches no declaration: an interaction is between two named
things in the code.

Measured 2026-09-24:

| citation | before | now |
| --- | --- | --- |
| `get_current_user` @ `aquiles/main.py:17` (the import) | LOCAL `main.py:17` | METHOD `jwt_aq.py:50` |
| same, @ `main.py:323` (a `Depends` default, absent from the graph) | LOCAL `main.py:17` | METHOD `jwt_aq.py:50` |
| `Authenticator` @ `src/fastapi_aad_auth/__init__.py:1` | LOCAL there | TYPE_DECL `src/fastapi_aad_auth/auth.py:28` |
| `_Authenticator` (aliased) @ `oauth/aad.py:3` | LOCAL `aad.py:3` | TYPE_DECL `auth.py:28` |
| `Depends` @ `aquiles/main.py:1` | LOCAL `main.py:1` | external |
| `notBlankStringValidation` @ static import line | not located | METHOD `ActionValidatorsUtils.java:56` |
| `ActionExecutionResult` @ type import line | not located | TYPE_DECL `ActionExecutionResult.java:22` |


## `declarationOf`

**The CPG's own scope link decides what a name means**, and the walk follows it
all the way. A use of a name has a REF edge to the variable it means, resolved by
scope -- a local wins inside its function, exactly as in Python. A name used in
a nested scope (a method using a module-level import) REFs a closure capture in
each scope between, so `declarationOf` follows those out to the variable
captured; `definitionsBehind` then follows an import to where it points.

```
qdrantwr.py:48   allow_metadata (inside _build_qdrant_filter)  -> PARAM allow_metadata (line 33)           local wins
qdrantwr.py:193  allow_metadata (inside query)                 -> capture -> <body> capture -> import (line 8) -> models.py:181
exchange.py:1039 amount_to_precision(...) (inside the method   -> capture -> <body> capture -> import (line 82) -> exchange_utils.py:215
                 Exchange.amount_to_precision)                     (a bare name skips the class scope)
```

Before this, the locator took the first REF only, treated the import binding it
landed on as "not a definition" and dropped it, then fell back to "any
declaration with this name in the file" -- which knows nothing of scope. So
`allow_metadata` at line 193 anchored on an unrelated function's parameter, and
`amount_to_precision` at 1039 on the method it is called from. The fallback now
also comes after the file's own import: a name inside a multi-line import
(`exchange.py:88`, recorded by the graph at 82) has nothing on its line to
follow.

**Calls.** A call's target edges come from a separate pass that guesses, and it
can disagree with the scope link. For a call made through a plain name,
`namedByScope` takes the method the name means by scope and `calleesOf` uses it
first. Measured 2026-09-24 on freqtrade's 4692 such calls: 3346 agree with the
frontend, 1327 were stub-only (the rescue above), 3 picked one of two guesses
(`amount_to_precision`, `price_to_precision`, `amount_to_contract_precision` in
`exchange.py`), and 16 overrode the frontend's single answer -- checked by hand,
the frontend was wrong: `get_timerange(...)` in `test_backtest_one` was linked
to nested helpers inside *other* test functions, and `disable_database_use(...)`
inside the method of that name was linked to the method itself (recursion)
rather than the imported function. Java is unaffected: its bare calls have the
receiver `this`, so no Java call is resolved this way.


## `fieldsOf`

A field use is resolved to the field's declaration, as a call is to its method.
Until 2026-09-24 it was not resolved at all, so **a role located on a field was
never reached**: the walk's `data` edges followed an identifier's reference to
its declaration, and a field use is not an identifier. Every spelling of it is an
`<operator>.fieldAccess` -- measured, 1 of 2,268 repository fields on chutney
and 0 of 1,972 on duffy is ever referenced any other way:

| source | graph |
| --- | --- |
| `repo.find(x)` in Java (no `this`) | `this.repo`, object typed as the enclosing class |
| `this.repo` / `result.duration` | object typed as its declared class |
| `Config.KEY` | a type reference to `Config` |
| `self.store` in Python | object `self`, typed `ANY` |

So the object's type names a class, and the field is that class's member of that
name -- or the nearest parent's (`inheritsFromTypeFullName`), where a level with
several is a guess among bases and resolves nothing. The class's own level keeps
every match: PYTHONSRC records a field once per assignment, so `self.url`
assigned at two lines is two members of one field. When the graph leaves the
object untyped, the profile answers `LangSpec.fieldOwnerType`: Python's `self`
and `cls` are the first parameter of a method and mean the class it is defined
in (PYTHONSRC typed `self` on 0 of 292 accesses on `CentOS_duffy`); Java types
every object and answers None.

One trap: PYTHONSRC declares a placeholder TYPE_DECL `ANY` (file `N/A`, not
marked external), so "is this type in the repository?" must require a real
source file, or every untyped `self` looks typed as `ANY` and resolves nothing.

Used in two places: the walk emits a `data` edge when the field carries a role,
and `occurrencesAt` resolves a citation on a line that *uses* a field -- which
may be declared in another file or a parent class.

Measured 2026-09-24 (field uses resolved to a repository field; the Python rest
are mostly library attributes like `logging.getLogger`):

| repo | field uses | resolved | via a parent class |
| --- | --- | --- | --- |
| `Enedis-OSS_chutney` | 7605 | 6845 | 0 |
| `KaiserpfalzEDV_kp-commons` | 1500 | 1442 | 0 |
| `freqtrade_freqtrade` | 46127 | 9687 | 490 |
| `djpugh_fastapi_aad_auth` | 1960 | 493 | 17 |
| `CentOS_duffy` | 6099 | 755 | 38 |
| `Aquiles-ai_Aquiles-RAG` | 1558 | 214 | 0 |

| citation / roles | before | now |
| --- | --- | --- |
| Enforcer `execute`, Store field `finallyActionRegistry` (used bare) | nothing | `Enforcer → Store  SelfRegisteringFinallyAction.java:31  data` |
| Store field `consumeDuration` of another object's class | nothing | `Enforcer → Store  QueueingConsumer.java:60  data` |
| Store `static final CONTENT_TYPE` | nothing | `Enforcer → Store  AmqpBasicPublishAction.java:97  data` |
| Store `self.fake_api_tenant` (Python) | nothing | `Enforcer → Store  admin.py:44  data` |
| `consumeDuration` cited where it is used, in another file | unlocated | the field, `QueueingConsumer.java:123` |


## `occurring`

The object *occurring* at the annotated location. The citation is a location,
and a location names an occurrence -- which may be the declaration or may be a
use. Both denote the same object, so both are accepted and a use is followed to
what it refers to: an identifier's `REF` edge lands on the LOCAL, MEMBER or
METHOD_PARAMETER_IN that declares it, and a call site resolves to the method it
targets. This is the precise reading of a citation, and it is tried before
anything name-based. It settles the case that no rule over declarations can:
`db` has 16 METHOD_PARAMETER_IN nodes in `accounts.py`, one per function taking
a session, so its *declaration* line is ambiguous by name alone -- while a line
where `db` is used belongs to exactly one of them. Citing where the object is
seen doing something is also what a person citing the role does by instinct. Scoped to the
cited name, not swept globally: `nameExact` is indexed, so this is two lookups
per role rather than a pass over the 158k identifiers a mid-sized repository
carries.


## `preference`

One line now -- `Lang.declKindRank(e.kind)`. The ranking moved to the profiles
because the ties it breaks are *made by the frontend*: one source declaration can
yield several nodes of different kinds on one line, and which of them the
programmer meant depends on how that frontend lowers declarations. The
justification below was always Python's, which is the tell.

The cited line is taken as correct. A recovery cites an entity's own declaration
line -- the `def`/`class`/assignment line, not a body span -- and that is
exactly what the CPG records as the node's `lineNumber`, so the match is an
equality, not a search. This matters more than it looks: `(name, file)` is *not*
unique. Measured on this repo, 62 of 100 `(name, file)` pairs carry more than
one entity, because locals are scoped and reuse names freely -- `jwt` is
declared at both line 18 and line 111 of one file, and they are different
variables. The line is the only thing that separates them. No nearest-match
fallback. A citation whose line matches nothing is a disagreement between the
annotation and the graph; reporting the role as unlocated makes that visible,
where picking the closest same-named entity would bury it behind a confident
wrong answer. A METHOD wins a tie, because Python's frontend emits a TYPE_DECL
for a *function* as well as for a class -- both sit on the `def` line, and the
method is the one with a body. A class citation is unaffected: no METHOD carries
the class's name (its members are `<body>`, `__init__`, ...). TYPE_DECL then
beats LOCAL, because Python also binds a class's *name* as a local at the
`class` line -- so a class citation matches both, and the type is what was
meant. LOCAL beats MEMBER, which are one declaration the frontend records twice.


## `ranked`

**A unique candidate wins without consulting the line at all.** The line was
load-bearing for one reason -- `(name, file)` is not unique -- so it is needed
exactly when that is true, and is pure risk when it is not. Measured, the risk
is not theoretical: JAVASRC reports `getByUsername`'s `lineNumber` as 42 in
`UserAccount.java`, where line 42 is `private String password;` and the method
is declared at 61. The annotation cites 61 and is correct, the graph is 19 lines
wrong, and exactly one method of that name exists in the repository -- so
requiring the line rejects the only possible answer in favour of none. Checkout
drift has the same shape: a CPG built from a later revision shifts every line in
a file while every name in it still matches. So the line only arbitrates between
candidates that are otherwise equally good. `preference` collapses the kinds
first (one Python `def` yields a METHOD, a TYPE_DECL and a LOCAL on one line;
the METHOD is meant), and only a genuine tie inside the winning kind -- 16
parameters named `db` in one file, two overloads of one method -- ever reaches
`Lang.anchors`.


## `ranked2`

Both readings of the location are available now, so they are tried in order of
how precisely each pins the object. (`ranked` is the one ordering: the second
identical `pool.sortBy(preference)` that used to sit here was deleted.) 1. a *declaration* sitting on the cited
line. Most precise, and it must come first: PYTHONSRC represents `def f` as
binding a METHOD_REF to a module-level LOCAL named `f`, so the def line is
simultaneously a declaration (the METHOD) and a use (the LOCAL). Letting the use
win anchors every Python function on a variable, which has no body, and the walk
then starts nowhere -- measured, it took this repo from 5 interactions to 0. 2.
an *occurrence* on the cited line, resolved to what it denotes. This is where a
citation on a use lands: `db` at the line it is read on names one of the 16
parameters sharing that name in the file. 3. a unique candidate by name, line
ignored. For when the graph's line space is not the source's -- delomboked Java,
a CPG built from a newer revision -- where the name still identifies the object
and the line cannot. 4. finally the line as a tie-break among equals, via
`Lang.anchors`.


## `Ownership` (`── which role owns which node`)

A plain index now: the lookup already returned the node the annotation named, so
there is nothing to reinterpret. The span-ownership rule that used to live here
-- "a role owns a line range, innermost wins" -- is gone with the method-only
conversion that made it necessary. A class citation lands on the TYPE_DECL, and
§3 expands that to the methods it declares, which is what the span trick was
approximating. A node maps to **every** role located on it, not one. One entity
routinely fills several roles at once -- `Comparator`, `Hasher` and `Encrypter`
all cited as `pwd_context.verify` is a deliberate claim about the code. Which of
them an edge is attributed to is decided at emit time, by the pattern; see
`pairsFor`.

`viaProvider`: a library role whose call sites lie inside an injection provider
also owns the provider. `Hasher` cited as `BCryptPasswordEncoder` on the line of
`@Bean passwordEncoder() { return new BCryptPasswordEncoder(); }` locates as call
sites of that constructor; owning the provider is what lets a
`passwordEncoder.matches(...)` elsewhere reach it. Only providers -- a library
role's sites in an ordinary method stay sites.


## `CallGraph` (`── 1. call resolution: which method does a call run?`)

Ordering used to live here: a BFS from the method entry assigned every node a
rank, and a method's calls were sorted by it. The walk (§3) now steps the CFG
directly, so the order it visits nodes in *is* the execution order and there is
nothing left to rank.

`targetsOf` answers *which method does this call run*, first answer wins: a
callee with a body (`call`); the repository's overrides of a bodiless one
(`dispatch`, **When the frontend links only the interface**); a profile route
(**Framework routes**); the repository's injection providers of the receiver's
type (`providersOf`). It returns a `Resolution` -- hops in order, each a list of
alternatives -- rather than one list, because a route's `inOrder` step is two
methods that both run, which a list of alternatives cannot say. `Entry.reachesFrom`
reads the same answer (`Resolution.all`), so what counts as reaching a role is one
definition for entry selection and for the walk.


## `isSynthetic`

A name the frontend generated: `<module>`, `<body>`, `<lambda>0` (PYTHONSRC);
`<init>`, `<clinit>`, `<lambda>N` (JAVASRC). No source identifier contains an
angle bracket, so one test serves both, and it lives here rather than on the
seam. No citation names one, and a role anchored on one would claim whatever the
generated scope spans -- for `<module>`, an entire file.


## `methodsPassedTo`

Methods handed to a call as arguments -- a callable the receiver will invoke
later: a decorator, a callback, a router registration. A framework given a
function invokes it, and *which* framework is not worth modelling:
`include_router(r, dependencies=[Depends(guard)])`, `add_middleware(Klass)` and
a decorator are one pattern.

**Only a `METHOD_REF` counts.** That node exists because the frontend resolved
the name to a method, so `referencedMethod` reads an answer rather than guessing
one. JAVASRC throws instead of returning nothing when the edge is absent, hence
the `Try`.

A plain `IDENTIFIER` argument is deliberately ignored. The frontend saw that name
and did *not* conclude it was a method; overriding that by searching the
repository is what emitted `Enforcer -> Proxy` off `tmp0.append(sn.pydantic_view)`.
See **What guessing cost** above -- four of six derived interactions
were false, and two more were real calls mislabelled because a false edge had
relocated the walk.


## `Walker` (`── 4. the walk: step the control-flow graph`)

The traversal is an execution, not a survey. It starts at a method's first CFG
node and follows CFG edges the way control actually moves, handling each node as
it arrives -- so ordering is not a sort applied afterwards, it *is* the order
the nodes were reached. A call descends into the callee at the moment the call
node is reached and returns to continue the caller, which is why a nested
interaction lands immediately after the call that caused it, the way a sequence
diagram reads. Branch-insensitive by construction: every CFG successor is
followed, so both arms of an `if` are executed. A per-body `visitedNodes` set makes
loops terminate.


## `visitedNodes`

The per-body visit set: `add` returns true only the first time a node is seen, so
a node is inspected once and a loop terminates. The stack mechanics around it --
why the traversal is iterative rather than recursive, and why successors are
pushed reversed -- are §3 of the narrative above, with the measured visit orders.


## `firstOccurrences` / `render` (`── 5. emit`)

Duplicate `From -> To` tokens collapse to their **first** occurrence, and the
survivors are ranked. What generates the duplicates, why first-occurrence is the
only safe choice, and what the dropped rows cost is §6 of the narrative above.

`anchoredRoles` carries three diagnostic fields `detector.py` does not read: `by`
(which reading of the citation found it -- the number that says whether the line
is still load-bearing), and `file` / `line` (where it was found, which differs
from the citation exactly when the lookup crossed files).

---

# Language profiles

`walk.sc` declares `LangSpec`; each profile answers it. Full reasoning here, a
brief docstring per member in the profile itself.


## `python.sc`


### `id`

Python profile for `walk.sc`. Sent as the second half of the script, after the
walk. See `LangSpec` there for what each member decides and why the seam is
restricted to pure functions. Everything here was inline in the walk until the
Java frontend needed different answers to the same three questions: where a
declaration's line is, how an import maps to a file, and whether a parameter can
carry a default the frontend threw away.


### `declKindRank`

METHOD first, because PYTHONSRC emits a TYPE_DECL for a *function* as well as
for a class -- both sit on the `def` line, and the method is the one with a body.
A class citation is unaffected: no METHOD carries the class's name (its members
are `<body>`, `__init__`, ...). TYPE_DECL then beats LOCAL, because Python also
binds a class's *name* as a local at the `class` line, so a class citation
matches both and the type is what was meant. PARAM sits on the same line as the
`def` that declares it, so it ties with METHOD and loses -- right, since a
citation on that line naming the function means the function -- and beats LOCAL
because where both exist for one name they are the same binding and the
signature is the more precise claim. LOCAL beats MEMBER, which are one
declaration the frontend records twice.


### `declares`

`member.startsWith(owner + ".")`. PYTHONSRC spells containment as a dotted
fullName path rooted at the file -- `file.py:<module>.Owner.member` -- for a
class's methods and a function's nested functions alike, so one prefix test
covers both places the walk needs it.


### `anchorsAt`

Equality, and deliberately nothing weaker. A Python citation records the `def` /
`class` / assignment line, and that is exactly what PYTHONSRC stores as the
node's `lineNumber` -- verified on `views/profile.py`, where
`@auth_router.get('/profile')` sits on line 16 and the method node for
`profile_get` reports 17, the `def`. A decorator does not move the line, so
there is nothing for a span to absorb. `(name, file)` is not unique -- 62 of 100
such pairs on one measured repo carry more than one entity, because locals are
scoped and reuse names freely -- so the line is the only thing separating them,
and widening the rule here would start merging distinct variables. A citation
whose line matches nothing is a disagreement between the annotation and the
graph, and reporting the role unlocated makes that visible where a nearest-match
would bury it behind a confident wrong answer.


### `moduleFiles`

No longer a `LangSpec` member -- the walk never asked it; only this profile's own
import resolution does. `a.b.c` read both as a module (`a/b/c.py`,
`a/b/c/__init__.py`) and as a symbol inside one (`a/b.py`, `a/b/__init__.py`),
because the entity spelling does not say which it is. A leading `.` is relative
to the importing file's own directory, one level up per extra dot. The paths are
from the repository root; `toGraphFile` maps each onto a graph file exactly, or
as the tail of exactly one, which is what a `src/` layout needs.


### `importedDefinitions` / `isImportBinding`

See § `pool`. The chain (`Index.chain`) follows `name` as `file` imports it, file
by file, until one defines it at module level -- a METHOD or TYPE_DECL whose
fullName ends `:<module>.name`, or a LOCAL of the `<module>` method that is not
itself an import. A `seen` set stops two modules re-exporting each other.
`methodForName` (dependency injection) uses the same chain, so a `Depends(f)`
imported through a package re-export resolves too.


### `entryEdges`

`def guard(cfg = Depends(get_config))` — the default is absent from the CPG
entirely, so this is where Python's dependency injection lives. Four steps, none
of them in `walk.sc`:

1. **read the source.** The join starts at the `def` line — PYTHONSRC records a
   method's `lineNumber` there and decorators do not move it — and runs until the
   parens balance, so a signature wrapped over four lines arrives whole.
2. **find each default.** The parameter list is split at its top-level commas and
   the parameter is the entry whose own name it is; its default is everything after
   the entry's first top-level `=`. Brackets are counted throughout, so an
   annotation (`tuple[User, APIKey]`) or a nested call stays intact. Every name the
   default refers to is a candidate -- not the words of its string literals, nor
   its keyword-argument names (`Query(None, description="User ID")` offers `Query`
   and `None`, not `User` or `description`). Deliberately not keyed on `Depends`: a
   default naming a repo function is a binding whatever wrapped it.
   Until 2026-10-08 a regex found the default: it allowed no comma in the annotation,
   so `auth: tuple[User, APIKey] | None = Depends(get_current_user)` bound nothing
   (`contextfs`'s only Enforcer -> Validator link), had no word boundary (`token`
   matched inside `bearer_token`), and read string words as names, which bound
   `description="User ID in the request parameters"` to whatever repository function
   one of those words named (`amphetamarina_litellm`'s 25-helper `Enforcer -> Key
   manager`; `bluesky_tiled`'s `Enforcer -> Validator` through a test fixture).
3. **resolve the name** through `methodForName` (below).
4. **return** (target, the `def` line, `dependency-injection`). The walk emits
   these before the body's first node, because a parameter is bound first.

Nothing runs until a walked method actually carries a default, and the indexes
behind step 3 are built on first use, so a repo with none pays nothing.


### `methodForName`

The method a DI name refers to. Three shapes, tried in order:

| | |
| --- | --- |
| `Depends(get_current_user)` | the name *is* a function, and this file's import says which module defines it |
| `Security(auth_scheme)` | the name is a variable holding an instance, and what the framework invokes is the type's `__call__`, not the variable |
| `_dep = [Depends(f)]` | the name is a variable holding the wiring, so what matters is inside its value |

The last is the idiom a shape-matching source scanner could never follow: the one
repo carrying it hoists the list into a variable, so no `Depends(` appears at the
registration site at all. Unlike a parameter default that assignment *is* in the
CPG — an ordinary statement in a body — so it is a graph hop, not a source read.

Six lazy indexes sit behind it: imports by (file, local name), methods by file,
methods by bare name, methods by owner, type declarations by both spellings of
their fullName, and assignments by the name assigned to. Each replaced a scan
performed once per name; the second shape is the one that still scans, so its
answer is kept per name (`namedTypes`), and the whole lookup per (name, file,
depth) (`nameMemo`). The profile's other per-call answers are kept the same way --
`importedCallees` per call, `classOfObject` per node, `classesHeld` per variable,
a field's classes per (field, owner) -- all pure functions of the graph, which the
walk otherwise recomputed for every call it passed.


### `scopedHit`

Ordering, not suppression. An earlier cut switched the repository-wide lookup off
whenever the file merely *had* an import for the name — which is wrong for any
language whose `moduleFiles` cannot map an entity to a path: the import branch
finds nothing and the fallback is gone too. Import-scoped still wins whenever it
resolves, which is where the precision comes from; the global lookup only runs
when it does not. And it only answers when one method in the repository has the
name: it used to take the first of several, which is some other file's function
chosen by storage order.



### `invokedOnInstance`

`Security(auth_scheme)` invokes the type's `__call__`, not the variable.


### `importedCallees`

A bare call -- receiver is the identifier with the call's own name -- resolved
through `importedDefinitions`. `importedTarget` (which answered `None` here) is
gone from the seam: it was switched off because the only Python resolver was a
one-file guess, and the import chain replaced that guess. See **The one rescue**.

**A call on an annotated parameter** (`declaredTypeCallees`). PYTHONSRC types
`svc: AbstractApiKeyService` from the annotation but keeps the name as written, so
`svc.verify_key(...)` is addressed as `AbstractApiKeyService.verify_key` -- a
bodiless stub -- while the method is
`src/keyshield/services/base.py:<module>.AbstractApiKeyService.verify_key`. It is
not the `src/` layout: a CPG built from `src/` addresses the call the same way.
When the address is `T.m` with `T` a plain name, `T` is followed through the
file's import (else a class the file defines) and the answer is every `m` a run
could land on: the first definition up `T`'s bases, depth-first in declared order
(the MRO under single inheritance), then each subclass override, level by level.
Bases come from `inheritsFromTypeFullName`, which PYTHONSRC does resolve to full
names. Which of them the call runs is the walk's to decide, by trial -- see
**Locating the method to continue in**; the profile only names the candidates.
Only the plain shape is read: `svc_factory.<returnValue>.m`,
`__builtin.list.…` and file-qualified addresses are left alone.

Measured 2026-09-26 on `Athroniaeth_keyshield`, both annotated mappings: from 0
interactions to `Enforcer → Validator api.py:382` (obscure token) and `Enforcer →
Verification manager api.py:382`, `Verification manager → Comparator base.py:443`
(password). Nine other mappings over seven repos (`CentOS_duffy`,
`Aquiles-ai_Aquiles-RAG`, `freqtrade_freqtrade`, `Enedis-OSS_chutney`,
`KaiserpfalzEDV_kp-commons`, `apache_streampipes`, `catch-money_java-impetus`)
derive identically. Still lost: a call whose address is `<unknownFullName>`
(`self._hasher.verify`, `entity.ensure_valid` in `_verify_entity`), where the
frontend's candidate stubs include `ApiKeyHasher.verify` and `ApiKey.ensure_valid`
but the address itself names no type.

**A call on an imported module** (`moduleMemberCallees`, 0.15.0). `from
extralit_server.contexts import accounts` then `accounts.get_user_by_api_key(...)`:
PYTHONSRC addresses the call to `extralit_server/contexts.py:<module>.accounts.get_user_by_api_key`
-- a member of a file `contexts.py` that does not exist, `contexts` being a
package -- and marks it external, so the Enforcer never reached the Validator. The
call's object is a module-level name the file binds by import; the profile reads
that import's entity *as a module* (`contexts/accounts.py`, `contexts/accounts/__init__.py`
-- the first two of `moduleFiles`) and answers the called name as that file defines
it, or re-exports it through `chain`.

**A call on a field the graph leaves untyped** (`fieldValueCallees`, 0.15.0).
`self.x.m()` is lowered to `tmp0 = self.x; tmp0.m()`, and `tmp0` is `ANY` whenever
the frontend did not carry a type into the field -- which it does for `self.x = x`
from an annotated `__init__` parameter, and does not for `self.x: T = factory()`
(the annotation is dropped) or for a value assigned from another class. The
profile follows the temporary back to `self.x`, takes the class around the method
(`classAround`: the longest fullName prefix that is a class, so a closure inside a
method belongs to the method's class), and collects every write `obj.x = value` in
the repository whose `obj` is that class or one of its bases: the class is read
from the write's annotation on its source line (`self.x: T = ...`; `Optional[T]`,
`T | None` and quotes read as `T`), else from the value -- `self` (the class around
the writing method), a name typed as a repository class, or a constructor call
`T(...)`. Each class found answers `m` as `declaredTypeCallees` does (up the bases,
then each override); more than `MAX_FIELD_CLASSES` (3) answers nothing. Every
target is a write in the repository; nothing is matched by name alone.
`repoClasses` excludes PYTHONSRC's function TYPE_DECLs, the `<meta>` twin of every
class and placeholder types such as `ANY`, which is internal and would otherwise
read an untyped `self` as class `ANY`.

Both run only for a call whose callees the graph left wholly external
(`rescuedCallees`), after `declaredTypeCallees`, so a call the frontend linked is
never touched. Measured 2026-10-06 on 50 Python records (two models,
scoring 0.14.0 → 0.15.0): 46 unchanged; `Extralit_extralit` (ground truth) 0 →
2 interactions, `Enforcer → Validator` through `accounts.get_user_by_api_key`;
`stateset_stateset-agents` 1 → 4 on both sides; `codemation_easyauth` 0 → 2 / 3
through `self.parent.decode_token`, whose `parent` the code assigns an
`EasyAuthServer` (server.py:84) and an `EasyAuthClient` (client.py:97);
`Percolation-Labs_percolate` gpt-6.1-sol side 1 → 3. `engine-agi_engine-core`'s
`self.coordination_strategy.coordinate_execution` now resolves through the
annotation to `CoordinationStrategy` and its three implementations; its scores did
not change.

**Four more ways a repository class is lost** (unreleased, 2026-10-08). Each is a
binding the code states and the frontend did not carry; each is resolved the same
way, by binding a name through the calling file's own import (re-exports included,
`classNamed` / `chain`), never by name alone.

- **An object typed only by its annotation** (`classOfObject`). PYTHONSRC types an
  annotated parameter with the annotation's bare text, not the class's fullName:
  `login_access_token(crud: CRUDInterface = Depends(...))` types `crud` as
  `CRUDInterface`, so `crud.user.authenticate(...)` -- addressed to the stub
  `CRUDInterface.<member>(user).authenticate` -- found no owner for `user`.
  `classOfObject` now reads such a type with `annotationClass` and binds it; exactly
  one class or nothing. That serves both of its uses: the owner of `x.field.m()`, and
  a write `self.x = x` from an annotated parameter.
- **A module-level variable** (`moduleVariableCallees`). `from app import crud`, then
  `crud.user.authenticate()`, where `crud_user.py` has `user = CRUDUser(User)` and
  `crud/__init__.py` re-exports it -- the FastAPI template's layout -- or
  `authService.verify_token()` with `authService` imported by name. The variable is
  found through the import (`moduleDefs`, `chain`) and `classesHeld` reads its
  classes per assignment as `classesWritten` reads a field's: the annotation
  (`authService: AuthService = ...`), else the constructor call or a typed name.
  A name the enclosing function binds itself is not the module's variable.
- **A class addressed under a file the graph does not have** (`declaredTypeCallees`).
  `AuthManager.authenticate_user` imported by `from ..security.auth import
  AuthManager` is addressed `//security/auth.py:<module>.AuthManager.authenticate_user`;
  under a `src/` layout the address drops `src/`. The class name in such an address
  is still the one the file imports, so it is bound like a bare `T`.
- **A dependency read from a signature** -- see `entryEdges` step 2.
- **A value the code builds rather than constructs** (`classesOfValue`). One reader now
  answers "which repository classes does this expression hold" for fields
  (`fieldClasses`, `classesWritten`), module variables (`classesHeld`) and a function's
  own variables (`localValueCallees`, a new rescue, last in order): a constructor call,
  a typed or annotated name, either side of `a or b` / `a if p else b`, the items of a
  list, tuple or set, a field, a local's own assignments, a block's last expression;
  `x.__iter__()` is `x` and `x.__next__()` the elements of `x`, which is how a `for`
  variable is followed (`for b in self.backends` is lowered to `tmp0 = {tmp1 =
  self.backends; tmp1.__iter__()}`, `b = tmp0.__next__()`). A lookup already under way
  answers nothing (`guarded`), so `self.x = self.x or T()` terminates.
  When PYTHONSRC drops a parameter's annotation -- `x: T | None` is typed `ANY`,
  `x: Optional[T]` bare `typing.Optional` -- `classOfObject` reads it from the signature
  (`sourceAnnotation`). `allen0099_FastAPI-CacheX`'s `self.session_manager =
  session_manager or SessionManagerProxy.get()` now yields `SessionManager`, so the
  middleware's `__call__` reaches `get_session` (`middleware.py:328`); `FastJango`'s
  `backend.authenticate(request)` in `for backend in self.backends` reaches the three
  backends' `authenticate` through `[self.session_backend, self.token_backend]` and
  their `x or SessionAuthenticationBackend()` writes.
  `boundInFunction` no longer reads a closure binding id as a capture by itself: PYTHONSRC
  gives an `async def`'s `for` variable one, and a capture is never assigned in the function
  (`assignedLocally`).

Measured 2026-10-08 on 50 Python ground-truth mappings, against 0.16.0: 40 identical; `molar` 0 -> 4, `JBEI_dnada` 0 -> 4, `yaml-to-backend`
0 -> 4, `olaflaitinen_Promethium` 0 -> 3, `nijaru_zenith` 0 -> 3, `contextfs` 0 -> 2,
`SINDIT20` 0 -> 2; `amphetamarina_litellm` 4 -> 3 and `bluesky_tiled` 5 -> 3, both
losing edges read from string words (now `tiled`'s Enforcer reaches the Validator by
its own call at `authentication.py:694`). `Code-Index-MCP` and
`agent-governance-toolkit` exceed the tunnel's 100 s before and after. Without the
memo tables `litellm` went from 47 s to past 100 s; with them and the string rule it
derives in 24 s. Adding `classesOfValue` changed one of the 50 records, `FastJango` 0 -> 3 (its
`Verifier -> Session manager` rests on the frontend's own resolution of a dict's `.get` to
`SessionStore.get`, not on this reader), and no derivation's time.


### `isModuleOrClassBody`

`<module>` (a file's top level) and `<body>` (a class body), confirmed on the
`CentOS_duffy` CPG: 108 `<module>`, 169 `<body>`. Each binds its `def`s as
`name = <METHOD_REF>` without running them. See `isCallArgument` for why the walk
must not enter those.


## `java.sc`


### `id`

Java profile for `walk.sc`. Sent as the second half of the script, after the
walk. See `LangSpec` there. Java differs from Python in all three of the ways
the seam exists for, and the first one is not a preference -- it is the reason a
Java repo derived nothing at all before this profile existed.


### `anchorsAt`

Containment, because equality is impossible here. JAVASRC anchors a method's
`lineNumber` at its **first annotation**, not at the declaration a reader sees.
Measured on `catch-money_java-impetus`: 40:    @AutoLog("user login model
[password]") 41:    @PostMapping("/up") 42:    public Result<String>
login(@RequestBody LoginRequest r) { the CPG reports `login` at line **40**,
`lineNumberEnd` 110, while the annotation cites 42. Under equality every
annotated method fails to locate, the role is reported external, and since
Spring puts an annotation on essentially every controller and service method,
the whole repository derives zero interactions. So a citation anchors when it
falls inside the node's span. This is containment, not nearest-match: the cited
line has to be *within* the declaration, so it cannot drift onto a neighbouring
symbol. Nodes with no span (`lineEnd` -1 -- parameters, locals, members) still
require equality, which is what the second clause preserves.


### `declKindRank`

The same order as the Python profile, for a different reason: JAVASRC makes far
fewer ties -- there is no TYPE_DECL per method and no local bound to a class
name -- so the ranking mostly never fires. It stays METHOD-first because where a
tie does occur the declaration with a body is still what a citation means.

GAP: nothing here separates two overloads of one method. They tie, and
`anchorsAt`'s containment test then decides on the line.


### `declares`

`member.startsWith(owner + ".")`. A JAVASRC method fullName is
`pkg.Owner.member:signature` -- the signature suffix sits *after* the member
name, so a dotted prefix test is still correct.

GAP: a nested type is spelled `Outer.Inner`, so `Outer` claims `Inner`'s methods
as well. Harmless for the walk today -- they are reached as the outer class's
role, a defensible reading of a citation on the outer class -- but it is not what
a `$`-separated frontend would do, and it is the reason this is a profile member
rather than a constant in the walk.


### `moduleFiles` (removed)

Removed from the seam; it was empty here, and this was the reasoning. A Java import names a
fully-qualified type (`org.example.auth.TokenService`) whose file sits under a
source root the entity does not mention -- `java-impetus-auth-
impl/src/main/java/org/example/auth/TokenService.java` in a multi-module build.
Mapping the entity to an exact repository path therefore needs the build layout,
and the walk looks its candidates up by exact key. Returning nothing leaves the
walk on its by-name fallback, which is exactly how Java resolved before the
import index existed -- correct, just without the disambiguation Python now
gets. Matching by path *suffix* would close it and is the obvious next step; it
needs a lookup the walk does not yet have.


### `entryEdges`

`Nil`. Java binds nothing at method entry that the frontend discards: there are
no parameter defaults, and `@Autowired` / constructor injection is not read here.
Since the whole of that machinery lives in `python.sc`, a Java query defines no
source reader, no name resolution and no index for either. Java does derive
`dependency-injection` edges, but at the call made on the injected object rather
than at entry -- see `providerAnnotation`.

GAP, for whoever reads Spring wiring one day: JAVASRC records a method's
`lineNumber` on its first **annotation**, so a text-reading implementation here
must skip the annotations before looking for the parameter list. Python's does
not, because it does not have to.



### `invokedOnInstance`

Not modelled. The Java analogue is a functional interface's single abstract
method, whose name varies by interface, so there is no fixed list.


### `importedCallees`

A bare call -- JAVASRC writes `f()` as `this.f()`, so the receiver is `this` --
resolved through `importedDefinitions`. In practice that is a static import,
whose address JAVASRC writes under the class the call appears in. This replaced
`importedTarget` (split the import at its last dot); see **The one rescue**.

It also reframes the `moduleFiles` gap above. Mapping a Java import does not need
the build layout, because it does not need the *file*: it needs the owning type,
and the import states it.


### `importedDefinitions` / `isImportBinding`

A lookup by fully qualified name, in the order Java itself resolves a simple
name:

1. **single import.** `import a.b.Foo` is the type `a.b.Foo` (`a.b.Foo$Inner`
   tried for a nested type, JAVASRC's spelling); `import static a.b.Utils.check`
   is every `check` declared by `a.b.Utils` -- all overloads, and a field of that
   name, left for the walk to rank.
2. **own package.** A type in the file's package needs no import; the package
   comes from the namespace block JAVASRC records per file.
3. **on-demand.** `import a.b.*` makes `a.b.Foo` visible; `import static
   a.b.Utils.*` makes `Utils`'s members visible. JAVASRC records both as
   `entity=a.b` / `a.b.Utils` with `as=*` and does not mark which is static, so
   `entity.Name` is tried as a type and then as a member. **Not recursive**:
   `a.b.*` does not reach `a.b.c.Foo`, so this stays an exact name. Two
   on-demand imports both supplying the name return both, and the walk accepts
   neither -- Java rejects that reference as ambiguous too.

`isImportBinding` is always false: JAVASRC binds no LOCAL for an import.


### `accessorsOf`

Lombok's `getX` / `isX` / `setX` for a cited field. The query service builds Java
CPGs with `--delombok-mode types-only`, because the default mode analyses the
delomboked source, whose pretty-printer drops blank lines and shifts every line
in the graph away from the checkout (on `catch-money_java-impetus`, 515 methods
off and `getByUsername` cited at 61 recorded at 42). `types-only` keeps the
lines true, but a generated accessor is then only a bodiless stub:
`userAccount.getPassword()` resolves to it, and the walk cannot step inside to
see the read of `password`. So a role cited on the field owns the stubs instead.

Only stubs, and only ones declared on the field's own class: a hand-written
getter has a body and is walked like any method, and the owner prefix keeps a
library's `getPassword` from matching. GAP: a field declared on a parent class
and reached through a subclass (`userAccount.getId()` for `BaseJapPojo.id`), and
`@Accessors(fluent = true)`, which names the getter `x()`.

**`fieldOfAccessor`** is the same mapping read backwards, for a citation of the
*use*: `getAuthorizedKeys` cited at the line that calls it. The locator reads such a
citation as the call on that line (`occurrence-at-line`) and takes the method it
runs -- and a generated accessor is a bodiless external stub, which no declaration
matches, so the role used to fall through to a library call: nothing in the
repository to walk from. `calledDecl` now hands a stub to
`fieldOfAccessor`, which returns field `x` of the class the stub is declared on
(`getX` / `isX` / `setX`, matched as `x.capitalize`), and the role is located on
that MEMBER -- exactly as if the field had been cited, so `accessorsOf` then gives
it every call to the accessor. Same limits: the field's own class only, no fluent
accessors; a library's accessor has no repository class and is still a library call.
Python has no generated accessors and returns Nil.

Measured 2026-10-03 on a 25-record Java batch: two citations
changed, both GPT's. `ctlabs-dev_auth-spring-boot-starter`, Token manager
`getAuthorizedKeys` @ `ClientKeyAuthenticationFilter.java:39` → MEMBER
`AuthProperties.ApiKeyAuth.authorizedKeys`, deriving `Enforcer → Token manager :39`
(Level 3a n/a → 1.0); `modulepub_tg-boot`, Key manager `getSecret` @
`JwtSupport.java:18` → `TgJwtProperties.secret` (n/a → 0.33). The other 23 records
are identical before and after.

Measured 2026-09-26 on `catch-money_java-impetus`, `login` → `password` (MEMBER
`UserAccount.java:42`): before, nothing; after, `Verifier → Password store
LoginApi.java:62 call`. The class-cited case (`UserSettings`, reached only by
`isSsoEnabled()`) and `CentOS_duffy` are byte-identical before and after.

Measured 2026-09-24: `VALID_NAME_PATTERN` in `HasNameSpace.java` (via
`import static …HasName.*`) → MEMBER `HasName.java:33`; `ConnectionFactoryFactory`
in `AmqpBasicConsumeAction.java` (same package, no import) → TYPE_DECL
`ConnectionFactoryFactory.java:24`; `Map` under `import java.util.*` → external.


### `routes`

One route: Spring Security's `ProviderManager`. A call to
`AuthenticationManager.authenticate` -- or on a receiver from
`getAuthenticationManager()`, `authenticationManager()` or
`authenticationManagerBean()`, which JAVASRC leaves untyped when a Spring
superclass declares them -- runs the first step that resolves:

1. `anyOf` **`AuthenticationProvider.authenticate`** -- the repository's own
   providers; Spring picks one by `supports()`, so they compete.
2. `inOrder` **`retrieveUser`, then `additionalAuthenticationChecks`** on a
   subclass of `AbstractUserDetailsAuthenticationProvider` or
   `DaoAuthenticationProvider` -- such a class does not override `authenticate`;
   the inherited template calls these two, in this order.
3. `inOrder` **`UserDetailsService.loadUserByUsername`, then
   `PasswordEncoder.matches`** -- no provider of the repository's own, so Spring's
   built-in `DaoAuthenticationProvider` runs: load the user, then compare. A target
   with no implementation falls through to its `@Bean` provider (a
   `BCryptPasswordEncoder`, typically).

The order is Spring's: its own `AuthenticationProvider`s are consulted before the
default one, and step 2 is a special case of what step 3 describes. Owner and
target types are fully qualified, matched by simple name as well because an
unresolved import can leave only that. The reasoning, the assumptions and the
measurement are §4's **Framework routes**.

GAP: `ReactiveAuthenticationManager` (WebFlux) is not routed -- its
`AuthenticationWebFilter` is itself library code, so there is usually no repository
filter to start from. Neither is the resource server's `jwtAuthenticationConverter`,
for the reason in the survey table.


### `providerAnnotation`

`Bean`. A `@Bean` method's return value is what Spring injects into every field of
that type, so a call on such a field runs the object the method built. JAVASRC
records the annotation on the METHOD node and the declared return type on its
`methodReturn`, so matching needs no source reading. Only the declared type is
matched: a `@Bean` declared as `BCryptPasswordEncoder` does not serve a
`PasswordEncoder` field. `@Component` classes need nothing -- they implement the
interface in the repository and `dispatch` already finds them.


### `constructorName`

`<init>`. `new BCryptPasswordEncoder()` is a call named `<init>` whose
`methodFullName` owner is the class, so a role citing the class by name matched no
call before. With it, `Locator.callSitesOf` also takes the constructor calls of
the cited class on the cited line (or in the method containing it), and a
constructor inside a `@Bean` makes the role the provider's (`Ownership`).


### `isModuleOrClassBody`

Always false. A Java class body is a TYPE_DECL, not a method the walk can be
inside, so there is no body whose `def`s bind without running. (This profile used
to answer `nestedDefOperator` with "", on the reasoning that Java's lambdas are
not definition statements -- true, and the reason a returned lambda was never
entered; see `isCallArgument`.)

