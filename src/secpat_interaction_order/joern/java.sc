/* Java profile for walk.sc. Reasoning and known gaps in JOERN.md § java.sc. */
object Lang extends LangSpec {

  val id = "JAVASRC"

  /** Containment: JAVASRC puts a method's line on its first annotation. */
  def anchorsAt(cited: Int, line: Int, lineEnd: Int): Boolean =
    cited == line || (lineEnd >= line && cited >= line && cited <= lineEnd)

  def declKindRank(kind: String): Int = kind match {
    case "METHOD"    => 0
    case "TYPE_DECL" => 1
    case "PARAM"     => 2
    case "LOCAL"     => 3
    case "MEMBER"    => 4
    case _           => 5
  }

  /** `pkg.Owner.member:signature`: containment is a dotted prefix. */
  def declares(owner: String, member: String): Boolean =
    member.startsWith(owner + ".")

  /** By fully qualified name, in Java's order: single import, own package, on-demand import. */
  def importedDefinitions(file: String, name: String, root: String): List[Long] = {
    val single = imports.get((file, name)).toList.flatMap(byQualifiedName)
    if (single.nonEmpty) single
    else {
      val samePackage = packageOf.get(file).toList.flatMap(p => types(p + "." + name))
      if (samePackage.nonEmpty) samePackage
      else onDemand.getOrElse(file, Nil).flatMap(e => byQualifiedName(e + "." + name)).distinct
    }
  }

  /** A bare call (receiver `this` or none), resolved through the file's imports. */
  def importedCallees(c: Call, root: String): List[Long] = {
    val bare = c.receiver.l match {
      case Nil                     => true
      case (i: Identifier) :: Nil  => i.name == "this"
      case _                       => false
    }
    if (!bare) Nil else importedDefinitions(norm(c.method.filename), c.name, root)
  }

  /** None: JAVASRC types every object a field is read from. */
  def fieldOwnerType(access: Call, root: String): Option[String] = None

  /** False: JAVASRC binds no LOCAL for an import. */
  def isImportBinding(local: Local, root: String): Boolean = false

  /** Lombok's `getX` / `isX` / `setX`: bodiless stubs declared on the field's own class. */
  def accessorsOf(field: Member, root: String): List[Long] = {
    val owner = field.typeDecl.fullName
    val cap   = field.name.capitalize
    val names = Set("get" + cap, "is" + cap, "set" + cap)
    cpg.method.isExternal(true).filter(m => names(m.name) && declares(owner, m.fullName)).id.l
  }

  /** `getX` / `isX` / `setX` stub -> field `x` of the class it is declared on, matched the way
    * `accessorsOf` names them (`x.capitalize`). A library's accessor has no repository class. */
  def fieldOfAccessor(accessor: Method, root: String): List[Long] = {
    val cut = accessor.fullName.indexOf("." + accessor.name + ":")
    val prop = List("get", "is", "set").collectFirst {
      case p if accessor.name.length > p.length && accessor.name.startsWith(p) &&
                accessor.name.charAt(p.length).isUpper => accessor.name.substring(p.length)
    }
    if (!accessor.isExternal || cut <= 0) Nil
    else prop.toList.flatMap { cap =>
      cpg.typeDecl.fullNameExact(accessor.fullName.substring(0, cut)).filterNot(_.isExternal)
        .member.l.filter(_.name.capitalize == cap).map(_.id)
    }
  }

  private def norm(p: String): String = p.replace('\\', '/').stripPrefix("./").stripPrefix("/")

  private def types(fullName: String): List[Long] =
    cpg.typeDecl.fullNameExact(fullName).filterNot(_.isExternal).id.l

  /** `owner`'s methods and fields called `name`. */
  private def membersOf(owner: String, name: String): List[Long] =
    cpg.method.nameExact(name).filterNot(_.isExternal)
      .filter(_.fullName.startsWith(owner + "." + name + ":")).id.l ++
    cpg.typeDecl.fullNameExact(owner).filterNot(_.isExternal).member.nameExact(name).id.l

  /** A type (`a.b.Foo`, or nested `a.b.Outer$Inner`) or a member (`a.b.Utils.check`). */
  private def byQualifiedName(entity: String): List[Long] = {
    val k = entity.lastIndexOf('.')
    if (k <= 0) types(entity)
    else {
      val (owner, last) = (entity.substring(0, k), entity.substring(k + 1))
      val asType = types(entity) ++ types(owner + "$" + last)
      if (asType.nonEmpty) asType else membersOf(owner, last)
    }
  }

  /** (file, importedAs, importedEntity) for every import. */
  private lazy val importRows: List[(String, String, String)] =
    cpg.imports.l.flatMap { i =>
      val f = norm(i.file.name.headOption.getOrElse(""))
      val as = i.importedAs.getOrElse("")
      val ent = i.importedEntity.getOrElse("")
      if (f.isEmpty || as.isEmpty || ent.isEmpty) None else Some((f, as, ent))
    }

  /** (file, simple name) -> entity, for single imports. */
  private lazy val imports: Map[(String, String), String] =
    importRows.filter(_._2 != "*").map { case (f, as, e) => (f, as) -> e }.toMap

  /** file -> the packages / types its on-demand (`*`) imports open. */
  private lazy val onDemand: Map[String, List[String]] =
    importRows.filter(_._2 == "*").groupBy(_._1).map { case (f, rs) => f -> rs.map(_._3).distinct }

  /** file -> its package. */
  private lazy val packageOf: Map[String, String] =
    cpg.namespaceBlock.l.filterNot(_.name == "<global>")
      .map(n => norm(n.filename) -> n.name).toMap

  val invokedOnInstance = List.empty[String]

  /** Never: a Java class body is a TYPE_DECL, not a method the walk can be inside. */
  def isModuleOrClassBody(m: Method): Boolean = false

  /** Spring hands a class an interface; the call runs whichever implementation it injected. */
  val dispatchesToOverrides = true

  /** Nil: Java has no parameter defaults, and Spring wiring is not read. */
  def entryEdges(m: Method, root: String): List[(Method, Int, String)] = Nil

  /** Spring's `@Bean`: the method's return value is what an injected field of that type holds. */
  override val providerAnnotation = "Bean"

  /** `new X()` is a call to `X.<init>`. */
  override val constructorName = "<init>"

  private val SEC = "org.springframework.security."

  /** Spring Security's `ProviderManager`: a login filter hands its token to the
    * `AuthenticationManager`, which passes it to a registered `AuthenticationProvider` -- the
    * repository's own, else a `UserDetails` provider's two template steps, else the built-in
    * `DaoAuthenticationProvider`, which loads the user and then compares the password. The
    * getters cover the usual receivers, which the frontend leaves untyped when the filter
    * inherits them from a Spring class. Reasoning in JOERN.md § Framework routes. */
  override val routes = List(
    Route(SEC + "authentication.AuthenticationManager", "authenticate",
      List("getAuthenticationManager", "authenticationManager", "authenticationManagerBean"),
      "[spring] ProviderManager.authenticate",
      List(
        RouteStep(inOrder = false, List(List(SEC + "authentication.AuthenticationProvider") -> "authenticate")),
        RouteStep(inOrder = true, List(
          List(SEC + "authentication.dao.AbstractUserDetailsAuthenticationProvider",
               SEC + "authentication.dao.DaoAuthenticationProvider") -> "retrieveUser",
          List(SEC + "authentication.dao.AbstractUserDetailsAuthenticationProvider",
               SEC + "authentication.dao.DaoAuthenticationProvider") -> "additionalAuthenticationChecks")),
        RouteStep(inOrder = true, List(
          List(SEC + "core.userdetails.UserDetailsService") -> "loadUserByUsername",
          List(SEC + "crypto.password.PasswordEncoder") -> "matches")))))
}
