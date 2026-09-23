"""Bilingual (English/Spanish) UI support.

Translations live in a plain Python dict rather than gettext ``.po``/``.mo``
files on purpose: the catalog is diffable in review, needs no compile step in
the Docker build, and cannot silently fall back to English because someone
forgot to run ``pybabel compile``. A missing key renders the English source
string, so an untranslated page degrades to readable English rather than to a
blank or a ``KeyError``.

Language is resolved per request, most explicit first:

1. ``?lang=`` on the URL (the toggle button) — also written to a cookie
2. the ``lang`` cookie (what this browser last chose)
3. the worker's saved ``Employee.language`` (so a Spanish-speaking worker's
   private links open in Spanish on a phone that has never been toggled)
4. ``DEFAULT_LANGUAGE``

Callers never pass a language around: templates call ``t("...")`` and the
active language comes from ``flask.g``.
"""
from __future__ import annotations

from urllib.parse import urlencode

from flask import g, has_request_context, request

DEFAULT_LANGUAGE = "en"

# Display names are in their own language, which is what a speaker looks for.
SUPPORTED_LANGUAGES: dict[str, str] = {"en": "English", "es": "Español"}

# Short label for the toggle button.
LANGUAGE_SHORT: dict[str, str] = {"en": "EN", "es": "ES"}

COOKIE_NAME = "lang"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # a worker's phone should remember this


def is_supported(lang: str | None) -> bool:
    return lang in SUPPORTED_LANGUAGES


def normalize(lang: str | None) -> str | None:
    """Accept 'es', 'ES', 'es-MX', 'es_MX' -> 'es'. Unknown -> None."""
    if not lang or not isinstance(lang, str):
        return None
    base = lang.strip().replace("_", "-").split("-")[0].lower()
    return base if is_supported(base) else None


def current_language() -> str:
    if not has_request_context():
        return DEFAULT_LANGUAGE
    return getattr(g, "language", DEFAULT_LANGUAGE)


def other_language() -> str:
    """The language the toggle button switches to."""
    return "es" if current_language() == "en" else "en"


def translate(text: str, lang: str, /, **kwargs: object) -> str:
    """Translate ``text`` into ``lang`` explicitly.

    Used where the reader is not whoever made the request — an SMS is written
    in the *worker's* language while the manager who pressed Dispatch may be
    reading the console in another.
    """
    rendered = CATALOG.get(lang, {}).get(text, text) if lang != DEFAULT_LANGUAGE else text
    if kwargs:
        try:
            return rendered.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text.format(**kwargs) if _formattable(text, kwargs) else text
    return rendered


def t(text: str, /, **kwargs: object) -> str:
    """Translate ``text`` into the active language.

    ``text`` is the English source string, so an untranslated key still reads
    correctly. Interpolation uses ``str.format`` named fields, e.g.
    ``t("Hi {name}", name=worker.first_name)``.
    """
    lang = current_language()
    rendered = CATALOG.get(lang, {}).get(text, text) if lang != DEFAULT_LANGUAGE else text
    if kwargs:
        try:
            return rendered.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            # A malformed translation must never 500 a worker's check-in page.
            return text.format(**kwargs) if _formattable(text, kwargs) else text
    return rendered


def _formattable(text: str, kwargs: dict) -> bool:
    try:
        text.format(**kwargs)
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Catalog. Keys are the exact English source strings passed to t().
# Spanish chosen for a Colorado arena crew: plain, neutral Latin-American
# usted-form, short enough for phone screens and 160-character SMS segments.
# --------------------------------------------------------------------------- #
CATALOG: dict[str, dict[str, str]] = {
    "es": {
        '(sets the year)':
            '(define el año)',
        '(~{hours} hrs)':
            '(~{hours} h)',
        '+ Create shifts':
            '+ Crear turnos',
        '1. Add a job':
            '1. Agregue un trabajo',
        '2. Add your workers':
            '2. Agregue a sus trabajadores',
        '3. Create, then invite':
            '3. Cree y luego invite',
        '4501 Brighton Blvd, Denver, CO':
            '4501 Brighton Blvd, Denver, CO',
        'A job is a work site or role you schedule shifts for — its title and address appear in every worker invitation.':
            'Un trabajo es un lugar o puesto para el que programa turnos; su título y dirección aparecen en cada invitación.',
        'Accept (first come, first served):':
            'Acepte (por orden de llegada):',
        'Actions':
            'Acciones',
        'Active':
            'Activo',
        'Active workers':
            'Trabajadores activos',
        'Add a job':
            'Agregar un trabajo',
        'Add a worker':
            'Agregar un trabajador',
        'Add job':
            'Agregar trabajo',
        'Add vendor':
            'Agregar proveedor',
        'Add worker':
            'Agregar trabajador',
        'Add your first job above — then you can schedule shifts for it.':
            'Agregue su primer trabajo arriba; después podrá programar turnos para él.',
        "Add your first worker above — they'll get shift invites by text.":
            'Agregue su primer trabajador arriba; recibirá las invitaciones de turno por mensaje de texto.',
        'Address':
            'Dirección',
        'Admin console':
            'Consola de administración',
        'Admin password':
            'Contraseña de administrador',
        'All shifts':
            'Todos los turnos',
        'All upcoming':
            'Todos los próximos',
        "All {total} spots were taken before you responded — it happens! You'll be invited to the next one.":
            'Los {total} lugares se ocuparon antes de su respuesta. Le invitaremos al siguiente.',
        'Already recorded':
            'Ya registrado',
        'Area (optional)':
            'Área (opcional)',
        'Area: {area}':
            'Zona: {area}',
        'Arrived within 5 minutes of start; paid from the start':
            'Llegó dentro de los 5 minutos del inicio; se paga desde el inicio',
        'Automatic email setup & messaging connections':
            'Configuración de correo automático y conexiones de mensajería',
        'Awaiting approval':
            'Pendiente de aprobación',
        'Build the schedule, send it to your workers, and watch seats fill in.':
            'Arme el horario, envíelo a sus trabajadores y vea cómo se llenan los lugares.',
        'CHECK IN':
            'ENTRADA',
        'CHECK OUT':
            'SALIDA',
        "Can't make it anymore? Contact your coordinator as soon as possible.":
            '¿Ya no puede asistir? Comuníquese con su coordinador lo antes posible.',
        'Cancelled':
            'Cancelado',
        'Check in':
            'Entrada',
        'Check in when you arrive: {url}':
            'Registre su entrada al llegar: {url}',
        'Check out':
            'Salida',
        'Check out of your other shift first':
            'Primero registre la salida de su otro turno',
        'Check-in / out':
            'Entrada / salida',
        'Check-in link':
            'Enlace de entrada',
        'Choose the exact dates. The time and headcount below apply to each selected date.':
            'Elija las fechas exactas. La hora y el número de personas de abajo se aplican a cada fecha seleccionada.',
        'Clear all':
            'Borrar todo',
        'Connecting the pipes':
            'Conexiones técnicas',
        'Contact your coordinator with any questions.':
            'Comuníquese con su coordinador si tiene preguntas.',
        'Copies every shift in the chosen week to the following week — then adjust as needed.':
            'Copia todos los turnos de la semana elegida a la semana siguiente; después ajuste lo que haga falta.',
        'Copy':
            'Copiar',
        'Copy link':
            'Copiar enlace',
        'Copy the schedule out of the email and paste it here. Each line like':
            'Copie el horario del correo y péguelo aquí. Cada línea como',
        'Copy the week of':
            'Copiar la semana del',
        'Copy to next week':
            'Copiar a la semana siguiente',
        'Create a job first':
            'Cree un trabajo primero',
        'Create a shift or paste an email schedule to get started.':
            'Cree un turno o pegue un horario del correo para empezar.',
        'Create shift':
            'Crear turno',
        'Create shifts':
            'Crear turnos',
        'Create {n} shifts':
            'Crear {n} turnos',
        'Creating shifts does not send messages. Review the schedule, then send invitations.':
            'Crear turnos no envía mensajes. Revise el horario y luego envíe las invitaciones.',
        'DONE':
            'LISTO',
        'Date':
            'Fecha',
        'Date: {date}':
            'Fecha: {date}',
        'Dates':
            'Fechas',
        'Deactivate':
            'Desactivar',
        'Dead phone':
            'Teléfono sin batería',
        'Default crew':
            'Cuadrilla predeterminada',
        'Default crew size':
            'Tamaño de cuadrilla predeterminado',
        'Default crew size pre-fills new shifts; you can override it per shift.':
            'El tamaño de cuadrilla predeterminado rellena los turnos nuevos; puede cambiarlo en cada turno.',
        'Delete':
            'Borrar',
        'Delete job & its shifts':
            'Eliminar el trabajo y sus turnos',
        'Delete shift':
            'Eliminar turno',
        'Discard drafts':
            'Descartar borradores',
        'Download CSV':
            'Descargar CSV',
        'Draft — review before sending':
            'Borrador: revise antes de enviar',
        'ERROR':
            'ERROR',
        'Email domain':
            'Dominio de correo',
        'Email sent on':
            'Correo enviado el',
        "Emails are only accepted from a vendor's own domain (DKIM verified).":
            'Solo se aceptan correos del dominio propio del proveedor (verificado con DKIM).',
        'End (optional)':
            'Fin (opcional)',
        'End time (leave blank if until done)':
            'Hora de fin (déjelo en blanco si es hasta terminar)',
        'Ends (blank = until done)':
            'Termina (en blanco = hasta terminar)',
        'Enter your 4-digit PIN to check in. It submits automatically.':
            'Escriba su PIN de 4 dígitos para registrar su entrada. Se envía solo.',
        'Enter your 4-digit PIN to check out. It submits automatically.':
            'Escriba su PIN de 4 dígitos para registrar su salida. Se envía solo.',
        'Everyone here receives shift invitations when you dispatch. Deactivate someone to pause their invites without losing their history.':
            'Todos los que aparecen aquí reciben invitaciones de turno cuando usted las envía. Desactive a alguien para pausar sus invitaciones sin perder su historial.',
        'Export timesheet':
            'Exportar hoja de horas',
        'Find a shift':
            'Buscar un turno',
        'First come, first served — confirm below to lock in your spot.':
            'Por orden de llegada: confirme abajo para asegurar su lugar.',
        'First name':
            'Nombre',
        'For initial setup only. To add a schedule today, use the paste box above.':
            'Solo para la configuración inicial. Para agregar un horario hoy, use el cuadro de arriba.',
        'Fri':
            'vie',
        'From':
            'Desde',
        'Fully staffed':
            'Completo',
        'Get directions':
            'Cómo llegar',
        'Getting started':
            'Para empezar',
        'Go to the Inbox':
            'Ir a la Bandeja de entrada',
        'Go to the Jobs page':
            'Ir a la página de Trabajos',
        'Hi {name}':
            'Hola {name}',
        "Hours from texted IN/OUT punches, in each venue's local time, with missing and late punches flagged.":
            'Horas de las marcas IN/OUT enviadas por mensaje, en la hora local de cada sede, con las marcas faltantes y tardías señaladas.',
        "I'm available — accept this shift":
            'Estoy disponible — acepto este turno',
        "I'm not available":
            'No estoy disponible',
        'IN':
            'ENTRADA',
        'If you can no longer make it, contact your coordinator ASAP.':
            'Si ya no puede asistir, hable con su supervisor lo antes posible.',
        'If your availability changes, contact your coordinator.':
            'Si cambia su disponibilidad, comuníquese con su coordinador.',
        'In at {time}':
            'Entrada a las {time}',
        'Inactive':
            'Inactivo',
        'Inbox':
            'Bandeja de entrada',
        'Invitation closed':
            'Invitación cerrada',
        'Job':
            'Trabajo',
        'Job default':
            'Predeterminado del trabajo',
        'Job these shifts are for':
            'Trabajo al que corresponden estos turnos',
        'Job title':
            'Título del trabajo',
        'Job, area or date':
            'Trabajo, área o fecha',
        'Jobs':
            'Trabajos',
        'Keep this private link to find your shift again.':
            'Guarde este enlace privado para volver a ver su turno.',
        'LINK NOT VALID':
            'ENLACE NO VÁLIDO',
        'LOCKED':
            'BLOQUEADO',
        'Language':
            'Idioma',
        'Language for this worker':
            'Idioma de este trabajador',
        'Last name':
            'Apellido',
        'Levy Restaurants':
            'Levy Restaurants',
        "Lines that weren't turned into shifts":
            'Líneas que no se convirtieron en turnos',
        'Location address':
            'Dirección del lugar',
        'Location: {address}':
            'Lugar: {address}',
        'Log in':
            'Iniciar sesión',
        'Log out':
            'Cerrar sesión',
        'Lopez':
            'López',
        'Manage':
            'Administrar',
        'Manual punch':
            'Marca manual',
        'Maria':
            'María',
        'Mobile number':
            'Número de celular',
        'Mon':
            'lun',
        'NO CONNECTION · TRY AGAIN':
            'SIN CONEXIÓN · INTENTE OTRA VEZ',
        'NOT AVAILABLE':
            'NO DISPONIBLE',
        'NOT CHECKED IN':
            'SIN ENTRADA',
        'Name':
            'Nombre',
        'Names and phone numbers for invitations.':
            'Nombres y números de teléfono para las invitaciones.',
        'Need help with your PIN?':
            '¿Necesita ayuda con su PIN?',
        'Need workers':
            'Faltan trabajadores',
        'Needs workers':
            'Faltan trabajadores',
        'Needs {n}':
            'Faltan {n}',
        'Needs {n} more':
            'Faltan {n} más',
        'Needs {n} — starts soon':
            'Faltan {n} — comienza pronto',
        'New shift available: {title}':
            'Nuevo turno disponible: {title}',
        'Next month':
            'Mes siguiente',
        'No dates picked yet — tap days on the calendar.':
            'Todavía no ha elegido fechas; toque los días en el calendario.',
        'No invitations created':
            'No se crearon invitaciones',
        'No invitations sent yet':
            'Todavía no se han enviado invitaciones',
        'No jobs yet':
            'Todavía no hay trabajos',
        'No matching shifts. Clear the search or choose All upcoming.':
            'No hay turnos que coincidan. Borre la búsqueda o elija Todos los próximos.',
        'No schedules imported yet':
            'Todavía no se ha importado ningún horario',
        'No upcoming shifts':
            'No hay turnos próximos',
        'No vendors yet.':
            'Todavía no hay proveedores.',
        'No workers yet':
            'Todavía no hay trabajadores',
        'OUT':
            'SALIDA',
        'One shift per line, for example:':
            'Un turno por línea, por ejemplo:',
        'Only {count} spot left!':
            '¡Solo queda {count} lugar!',
        'Open check-in':
            'Abrir entrada',
        'Open check-out':
            'Abrir salida',
        'Open check-out when leaving':
            'Abra la salida al terminar',
        'Opening the screen does not clock you in. Entering your PIN records the action shown.':
            'Abrir la pantalla no registra su hora. Su PIN registra la acción que aparece.',
        'Opens {when}':
            'Abre {when}',
        'Page not found':
            'Página no encontrada',
        'Past shifts':
            'Turnos pasados',
        'Paste a schedule':
            'Pegar un horario',
        'Paste an email schedule':
            'Pegar un horario del correo',
        'Paste the date, number of people and start time above. Review the drafts before sending invitations.':
            'Pegue arriba la fecha, el número de personas y la hora de inicio. Revise los borradores antes de enviar las invitaciones.',
        'People':
            'Personas',
        'Phone':
            'Teléfono',
        'Phone number can be typed any way — 303-555-0123 works fine.':
            'El número de teléfono se puede escribir de cualquier forma; 303-555-0123 funciona bien.',
        'Pick English or Spanish.':
            'Elija inglés o español.',
        'Pick at least one date before creating.':
            'Elija al menos una fecha antes de crear.',
        'Pick exact dates or paste the schedule from an email.':
            'Elija las fechas exactas o pegue el horario de un correo.',
        'Pick shift dates':
            'Elegir las fechas del turno',
        'Previous month':
            'Mes anterior',
        'Private link':
            'Enlace privado',
        'Questions? Contact your coordinator.':
            '¿Preguntas? Comuníquese con su coordinador.',
        'REFRESH':
            'ACTUALIZAR',
        'Reactivate':
            'Reactivar',
        'Read shifts':
            'Leer turnos',
        'Reason (optional)':
            'Motivo (opcional)',
        "Reminder: you're confirmed for {title}":
            'Recordatorio: turno confirmado: {title}',
        'Remove':
            'Quitar',
        "Repeat last week's schedule":
            'Repetir el horario de la semana pasada',
        'Reply STOP to opt out.':
            'Responda STOP para cancelar.',
        'Response recorded':
            'Respuesta registrada',
        'Responses':
            'Respuestas',
        'Roster':
            'Lista de personal',
        'SHIFT OVER':
            'TURNO TERMINADO',
        'SMS check-in / out (Twilio)':
            'Entrada y salida por SMS (Twilio)',
        'STILL ON SHIFT':
            'AÚN EN TURNO',
        'SYSTEM DOWN':
            'SISTEMA CAÍDO',
        'Sat':
            'sáb',
        'Save':
            'Guardar',
        'Save changes':
            'Guardar cambios',
        'Schedule text':
            'Texto del horario',
        'Schedules received':
            'Horarios recibidos',
        'See your supervisor':
            'Hable con su supervisor',
        'Select a job…':
            'Seleccione un trabajo…',
        'Send the weekly schedule':
            'Enviar el horario semanal',
        'Send week to workers':
            'Enviar la semana a los trabajadores',
        'Sending again re-sends invitations to workers who have not replied. Confirmed workers keep their spots.':
            'Al enviar de nuevo se reenvían las invitaciones a quienes no han respondido. Los trabajadores confirmados conservan su lugar.',
        'Sends a separate invitation for each open shift in the selected seven days. Workers choose which shifts to accept.':
            'Envía una invitación por separado para cada turno abierto en los siete días seleccionados. Los trabajadores eligen qué turnos aceptar.',
        "Set the destination to the vendor's webhook URL with Basic Auth credentials embedded:":
            'Configure el destino con la URL del webhook del proveedor e incluya las credenciales de Basic Auth:',
        "Set the number's incoming-message webhook (HTTP POST) to:":
            'Configure el webhook de mensajes entrantes del número (HTTP POST) así:',
        'Shift confirmed':
            'Turno confirmado',
        'Shift full':
            'Turno lleno',
        'Shift offer':
            'Oferta de turno',
        'Shifts':
            'Turnos',
        'Shifts starting within 48h that still need workers are highlighted':
            'Se resaltan los turnos que comienzan en menos de 48 h y aún necesitan trabajadores',
        'Show':
            'Mostrar',
        'Show original email':
            'Ver el correo original',
        'Sign in':
            'Iniciar sesión',
        'Sign in to the admin console':
            'Inicie sesión en la consola de administración',
        'Something went wrong':
            'Algo salió mal',
        'Staffing':
            'Personal',
        'Start':
            'Inicio',
        'Starts':
            'Comienza',
        'Status':
            'Estado',
        'Sun':
            'dom',
        'Switch language to':
            'Cambiar idioma a',
        'TOO EARLY':
            'MUY TEMPRANO',
        'TOO MANY TRIES':
            'DEMASIADOS INTENTOS',
        'TRY AGAIN':
            'REINTENTAR',
        'Tap to accept (first come, first served):':
            'Toque para aceptar (por orden de llegada):',
        'Thanks for your quick reply — keep an eye out for the next invite.':
            'Gracias por responder rápido. Esté pendiente de la próxima invitación.',
        'Thanks {name} — this spot is yours. See you there.':
            'Gracias {name}: este lugar es suyo. Nos vemos allá.',
        'Thanks, {name}':
            'Gracias, {name}',
        "The page you're looking for doesn't exist or the link has expired. If you got here from a text message, contact your coordinator.":
            'La página que busca no existe o el enlace ya venció. Si llegó aquí desde un mensaje de texto, comuníquese con su coordinador.',
        'The position must match a job title.':
            'El puesto debe coincidir con el título de un trabajo.',
        'The venue and address where people will work.':
            'La sede y la dirección donde trabajará la gente.',
        'This invitation is no longer active':
            'Esta invitación ya no está activa',
        'This is a draft from {source}. It cannot be sent to workers until it is approved on the Inbox page.':
            'Este es un borrador de {source}. No se puede enviar a los trabajadores hasta que se apruebe en la Bandeja de entrada.',
        "This link isn't valid":
            'Este enlace no es válido',
        'This shift filled up':
            'Este turno se llenó',
        'This shift invitation is unavailable. Contact your coordinator for help.':
            'Esta invitación de turno no está disponible. Comuníquese con su coordinador.',
        'Thu':
            'jue',
        'Time':
            'Hora',
        'Time: {time}':
            'Hora: {time}',
        'Timezone':
            'Zona horaria',
        'Title':
            'Título',
        'To':
            'Hasta',
        'Try again':
            'Intente otra vez',
        'Try again in 15 min or see your supervisor':
            'Intente en 15 min o hable con su supervisor',
        'Tue':
            'mar',
        "Turn a vendor's schedule into shifts: paste it below (or have it emailed in), fix anything that needs it, then approve.":
            'Convierta el horario de un proveedor en turnos: péguelo abajo (o reciba el correo), corrija lo que haga falta y luego apruebe.',
        'Upcoming shifts':
            'Próximos turnos',
        'Use the button above to invite all active workers.':
            'Use el botón de arriba para invitar a todos los trabajadores activos.',
        'Vendor emails (SendGrid Inbound Parse)':
            'Correos de proveedores (SendGrid Inbound Parse)',
        'Vendors':
            'Proveedores',
        'Venue timezone':
            'Zona horaria de la sede',
        'View':
            'Ver',
        'View recorded hours':
            'Ver horas registradas',
        'View shift':
            'Ver turno',
        'WRONG PIN':
            'PIN INCORRECTO',
        'WRONG PIN · {n} LEFT':
            'PIN INCORRECTO · QUEDAN {n}',
        'Wait a minute':
            'Espere un minuto',
        'Warehouse crew':
            'Cuadrilla de almacén',
        'We hit an unexpected error. Please try again in a moment — if it keeps happening, contact your coordinator.':
            'Ocurrió un error inesperado. Intente de nuevo en un momento; si sigue pasando, comuníquese con su coordinador.',
        'Wed':
            'mié',
        'Week starting':
            'Semana que comienza',
        'What happens next':
            'Qué sigue',
        'When you arrive, open check-in and enter your 4-digit PIN.':
            'Cuando llegue, abra la entrada y escriba su PIN de 4 dígitos.',
        'When you finish, open the link again to check out.':
            'Cuando termine, abra el enlace otra vez para registrar su salida.',
        'Worker':
            'Trabajador',
        'Worker responses':
            'Respuestas de los trabajadores',
        'Workers':
            'Trabajadores',
        'Workers & timesheets':
            'Trabajadores y hojas de horas',
        'Workers needed':
            'Trabajadores necesarios',
        'Workers text IN on arrival (up to 60 min early) and OUT when leaving.':
            'Los trabajadores envían IN al llegar (hasta 60 min antes) y OUT al salir.',
        'Workforce dispatch & scheduling':
            'Despacho y programación de personal',
        'You need a job before you can import a schedule.':
            'Necesita un trabajo antes de poder importar un horario.',
        'You need a job location before you can create shifts.':
            'Necesita un lugar de trabajo antes de poder crear turnos.',
        "You're confirmed!":
            '¡Está confirmado!',
        "You're invited to a shift":
            'Le invitamos a un turno',
        "You're marked as not available for this shift.":
            'Quedó marcado como no disponible para este turno.',
        'Your PIN is usually the last 4 digits of your phone number. If that does not work, ask your supervisor to reset it. Do not share this link.':
            'Su PIN normalmente son los últimos 4 dígitos de su número de teléfono. Si no funciona, pida a su supervisor que lo restablezca. No comparta este enlace.',
        'Your first schedule, in three steps':
            'Su primer horario, en tres pasos',
        'a pasted schedule':
            'un horario pegado',
        'becomes a draft shift. End times and areas are only added when the line has them.':
            'se convierte en un turno borrador. La hora de fin y el área solo se agregan cuando la línea los incluye.',
        'confirmed':
            'confirmado',
        'declined':
            'rechazado',
        'draft':
            'borrador',
        'e.g. Parking':
            'p. ej. Estacionamiento',
        'levyrestaurants.com':
            'levyrestaurants.com',
        'manual':
            'manual',
        'not checked in':
            'sin registrar entrada',
        'on shift':
            'en turno',
        'waiting':
            'esperando',
        'workforce dispatch & scheduling':
            'despacho y programación de personal',
        '{day} at {time}':
            '{day} a las {time}',
        '{name} will now get pages and texts in {language}.':
            '{name} recibirá las páginas y los mensajes en {language}.',
        '{n} no':
            '{n} no',
        '{n} waiting':
            '{n} esperando',
        '{n} yes':
            '{n} sí',
        '{shown} of {total} shifts shown':
            'Mostrando {shown} de {total} turnos',
        '{taken} of {total} spots filled':
            '{taken} de {total} lugares ocupados',
        '~{hours} hrs':
            '~{hours} h',
    },
}


# --------------------------------------------------------------------------- #
# Request wiring
# --------------------------------------------------------------------------- #
def resolve_request_language(request) -> tuple[str, bool]:
    """Pick the language for this request.

    Returns ``(language, chosen)`` where ``chosen`` is True when the visitor
    (or their browser) named a language explicitly. Worker pages only fall
    back to the worker's saved language when ``chosen`` is False, so toggling
    to English on a shared phone is never overridden by the roster.
    """
    from_query = normalize(request.args.get("lang"))
    if from_query:
        return from_query, True

    from_cookie = normalize(request.cookies.get(COOKIE_NAME))
    if from_cookie:
        return from_cookie, True

    return DEFAULT_LANGUAGE, False


def browser_language(request) -> str | None:
    """Best supported match from the browser's Accept-Language header."""
    header = request.headers.get("Accept-Language", "")
    for part in header.split(","):
        code = normalize(part.split(";")[0])
        if code:
            return code
    return None


def set_language(lang: str) -> None:
    """Force the active language for the rest of this request."""
    if is_supported(lang):
        g.language = lang


def use_worker_language(employee) -> None:
    """Apply a worker's saved language unless the visitor already chose one."""
    if getattr(g, "language_chosen", False):
        return
    saved = normalize(getattr(employee, "language", None))
    if saved:
        g.language = saved


def register_i18n(app) -> None:
    """Resolve the language on every request and expose ``t()`` to templates."""

    @app.before_request
    def _pick_language():
        language, chosen = resolve_request_language(request)
        if not chosen:
            language = browser_language(request) or DEFAULT_LANGUAGE
        g.language = language
        g.language_chosen = chosen

    app.jinja_env.globals.update(
        t=t,
        current_language=current_language,
        other_language=other_language,
        switch_url=switch_url,
        format_date_long=format_date_long,
        format_date_medium=format_date_medium,
        format_date_short=format_date_short,
        calendar_months=calendar_months,
        calendar_day_abbr=calendar_day_abbr,
        calendar_month_abbr=calendar_month_abbr,
        supported_languages=SUPPORTED_LANGUAGES,
        language_short=LANGUAGE_SHORT,
    )


def switch_url(lang: str) -> str:
    """URL of the toggle that switches to ``lang`` and returns to this page.

    The ``lang`` query parameter is stripped from the return target. Leaving it
    in would out-rank the cookie the toggle just set and bounce the visitor
    straight back to the language they were trying to leave.
    """
    from flask import url_for

    query = [(k, v) for k, v in request.args.items(multi=True) if k != "lang"]
    target = request.path + (("?" + urlencode(query)) if query else "")
    return url_for("i18n.switch", lang=lang, next=target)


# --------------------------------------------------------------------------- #
# Dates
#
# strftime('%A, %d %B') emits English names whatever the active language, and
# the C locale is process-global and not thread-safe under Gunicorn, so the
# names are translated from explicit tables instead.
# --------------------------------------------------------------------------- #
_DAY_NAMES = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "es": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
}

_MONTH_NAMES = {
    "en": ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"],
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio",
           "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"],
}

_DAY_ABBR = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "es": ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"],
}

_MONTH_ABBR = {
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "es": ["ene", "feb", "mar", "abr", "may", "jun",
           "jul", "ago", "sep", "oct", "nov", "dic"],
}


def day_name(d, *, lang: str | None = None, abbr: bool = False) -> str:
    lang = lang or current_language()
    table = _DAY_ABBR if abbr else _DAY_NAMES
    return table.get(lang, table["en"])[d.weekday()]


def month_name(d, *, lang: str | None = None, abbr: bool = False) -> str:
    lang = lang or current_language()
    table = _MONTH_ABBR if abbr else _MONTH_NAMES
    return table.get(lang, table["en"])[d.month - 1]


def format_date_long(d, *, lang: str | None = None) -> str:
    """'Friday, 18 September 2026' / 'viernes, 18 de septiembre de 2026'."""
    if d is None:
        return ""
    lang = lang or current_language()
    if lang == "es":
        return f"{day_name(d, lang=lang)}, {d.day} de {month_name(d, lang=lang)} de {d.year}"
    return f"{day_name(d, lang=lang)}, {d.day:02d} {month_name(d, lang=lang)} {d.year}"


def format_date_medium(d, *, lang: str | None = None) -> str:
    """Same as long without the year: for cards where the year is obvious."""
    if d is None:
        return ""
    lang = lang or current_language()
    if lang == "es":
        return f"{day_name(d, lang=lang)}, {d.day} de {month_name(d, lang=lang)}"
    return f"{day_name(d, lang=lang)}, {d.day:02d} {month_name(d, lang=lang)}"


def format_date_short(d, *, lang: str | None = None, year: bool = False) -> str:
    """'Fri 18 Sep' / 'vie 18 sep' — for dense tables and SMS."""
    if d is None:
        return ""
    lang = lang or current_language()
    text = f"{day_name(d, lang=lang, abbr=True)} {d.day} {month_name(d, lang=lang, abbr=True)}"
    return f"{text} {d.year}" if year else text


def calendar_months(lang: str | None = None) -> list[str]:
    """Month names for the date-picker, in the page's language.

    The picker previously used ``toLocaleDateString``, which follows the
    *device's* locale — so a console set to Spanish could still label the
    calendar in whatever language the laptop happened to be.
    """
    lang = lang or current_language()
    return list(_MONTH_NAMES.get(lang, _MONTH_NAMES["en"]))


def calendar_day_abbr(lang: str | None = None) -> list[str]:
    """Short weekday names, Monday first (matching the picker's grid)."""
    lang = lang or current_language()
    return list(_DAY_ABBR.get(lang, _DAY_ABBR["en"]))


def calendar_month_abbr(lang: str | None = None) -> list[str]:
    lang = lang or current_language()
    return list(_MONTH_ABBR.get(lang, _MONTH_ABBR["en"]))
