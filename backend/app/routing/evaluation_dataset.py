"""Synthetic, deterministic routing cases for the KIBAK evaluation lab."""

from __future__ import annotations


def synthetic_cases() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    examples = {
        "Comercial": [
            ("Solicitud de tarifa para renovación", "Necesitamos precio y condiciones para renovar el servicio del próximo trimestre.", "consulta comercial"),
            ("Propuesta de colaboración", "Os enviamos una propuesta comercial para ampliar el acuerdo actual.", "propuesta comercial"),
            ("Condiciones de un nuevo cliente", "¿Podéis compartir las condiciones para empezar a trabajar con vosotros?", "alta comercial"),
            ("Negociación de volumen", "Queremos revisar el descuento aplicable al volumen anual previsto.", "negociación"),
            ("Consulta sobre catálogo", "Nos gustaría conocer las opciones disponibles para esta necesidad.", "consulta de producto comercial"),
            ("Adjunto propuesta firmada", "Adjuntamos la propuesta firmada para que reviséis los siguientes pasos.", "documentación comercial"),
        ],
        "Logística": [
            ("Entrega retrasada", "El envío previsto para ayer todavía no ha llegado y necesitamos localizarlo.", "incidencia de entrega"),
            ("Cambio de dirección de entrega", "Por favor, confirmad si podéis entregar el pedido en nuestra nueva dirección.", "gestión logística"),
            ("Seguimiento del transporte", "¿Podéis indicarnos el estado y la fecha estimada de llegada del transporte?", "seguimiento de transporte"),
            ("Mercancía dañada en recepción", "Hemos recibido dos cajas dañadas. Dejamos fotografías en el adjunto.", "incidencia de transporte", "Acta de recepción: dos bultos dañados"),
            ("Falta una caja", "El albarán indica cinco bultos, pero solo hemos recibido cuatro.", "faltante de entrega"),
            ("Horario de descarga", "Necesitamos coordinar el horario de descarga para el próximo envío.", "coordinación de entrega"),
        ],
        "Administración": [
            ("Factura pendiente de corregir", "La factura recibida tiene un dato fiscal incorrecto. ¿Podéis emitirla de nuevo?", "incidencia administrativa"),
            ("Certificado fiscal", "Solicitamos el certificado fiscal actualizado para nuestro expediente.", "documentación fiscal"),
            ("Estado de cuenta", "¿Podéis enviarnos el estado de cuenta y las facturas pendientes?", "consulta administrativa"),
            ("Cambio de datos fiscales", "Adjuntamos nuestros nuevos datos fiscales para actualizar la ficha.", "actualización fiscal", "Datos fiscales actualizados en PDF"),
            ("Consulta de vencimientos", "Necesitamos confirmar los vencimientos acordados para este mes.", "vencimientos"),
            ("Error en abono", "El abono aplicado no coincide con el importe que habíamos acordado.", "incidencia de abono"),
        ],
        "Compras": [
            ("Solicitud de compra", "Necesitamos adquirir material de oficina para el próximo mes.", "solicitud de compra"),
            ("Comparativa de proveedores", "Estamos comparando proveedores para este suministro y necesitamos vuestra ficha.", "homologación de proveedor"),
            ("Plazo de aprovisionamiento", "¿Cuál es vuestro plazo para reponer estas referencias?", "aprovisionamiento"),
            ("Pedido interno aprobado", "La solicitud interna ha sido aprobada. Podéis iniciar la compra indicada.", "compra aprobada"),
            ("Condiciones de suministro", "Queremos revisar las condiciones de suministro recurrente.", "condiciones de compra"),
            ("Adjunto solicitud técnica", "Adjuntamos las especificaciones para solicitar una oferta de compra.", "solicitud de oferta", "Especificaciones técnicas de compra"),
        ],
        "RRHH": [
            ("Solicitud de vacaciones", "Solicito vacaciones del 12 al 16 de agosto, quedo pendiente de confirmación.", "gestión de vacaciones"),
            ("Cambio de datos personales", "Necesito actualizar mi teléfono y dirección en el expediente laboral.", "datos de empleado"),
            ("Certificado laboral", "¿Podéis preparar un certificado de empresa para un trámite personal?", "certificado laboral"),
            ("Consulta de nómina", "Tengo una duda sobre el concepto variable que aparece en mi nómina.", "consulta de nómina"),
            ("Incorporación de persona", "Adjunto la documentación necesaria para preparar una nueva incorporación.", "alta de empleado", "Documentación de incorporación"),
            ("Formación obligatoria", "¿Cuándo se realizará la próxima formación obligatoria de prevención?", "formación interna"),
        ],
        "Dirección": [
            ("Resumen ejecutivo mensual", "Necesitamos el resumen de actividad y los principales riesgos del mes.", "informe ejecutivo"),
            ("Reunión de comité", "Confirmamos la reunión del comité y proponemos revisar las prioridades del trimestre.", "dirección y prioridades"),
            ("Decisión sobre proveedor estratégico", "Solicitamos validación para continuar con el proveedor estratégico propuesto.", "decisión estratégica"),
            ("Escalado urgente", "Esta incidencia afecta a un cliente clave y requiere una decisión de dirección hoy.", "escalado ejecutivo", None, "critical"),
            ("Plan anual", "Adjuntamos el borrador del plan anual para comentarios del equipo directivo.", "planificación"),
            ("Riesgo relevante", "Compartimos un riesgo relevante que puede afectar al servicio y requiere seguimiento.", "gestión de riesgos", None, "high"),
        ],
    }
    for department, rows in examples.items():
        for row in rows:
            title, body, category = row[:3]
            attachment = row[3] if len(row) > 3 and isinstance(row[3], str) else None
            criticality = row[4] if len(row) > 4 and isinstance(row[4], str) else "normal"
            cases.append(
                {
                    "title": title,
                    "subject": title,
                    "body": body,
                    "sender": f"remitente-{department.lower().replace(' ', '-') }@demo.invalid",
                    "expected_department": department,
                    "expected_category": category,
                    "expected_requires_review": False,
                    "criticality": criticality,
                    "attachment_text": attachment,
                }
            )
    cases.extend(
        [
            {
                "title": "Consulta con señales cruzadas",
                "subject": "Entrega y factura de una incidencia",
                "body": "El envío llegó tarde y además necesitamos revisar la factura asociada. ¿Quién puede ayudarnos?",
                "sender": "cliente-ambiguo@demo.invalid",
                "expected_department": "Logística",
                "expected_category": "caso ambiguo",
                "expected_requires_review": True,
                "criticality": "high",
            },
            {
                "title": "Mensaje sin departamento claro",
                "subject": "Una consulta general",
                "body": "Nos gustaría hablar con alguien sobre varias cuestiones del servicio, todavía no sabemos a qué equipo corresponden.",
                "sender": "cliente-incierto@demo.invalid",
                "expected_department": None,
                "expected_category": "sin clasificar",
                "expected_requires_review": True,
                "criticality": "normal",
            },
            {
                "title": "Exclusión: producto no operativo",
                "subject": "Referencia y disponibilidad",
                "body": "Preguntamos por la disponibilidad de una referencia y no por una entrega ya confirmada.",
                "sender": "cliente-exclusion@demo.invalid",
                "expected_department": "Comercial",
                "expected_category": "consulta comercial",
                "expected_requires_review": True,
                "criticality": "normal",
                "notes": "Debe respetar la exclusión logística configurada para consultas previas a la compra.",
            },
            {
                "title": "Adjunto ilegible",
                "subject": "Documento escaneado para revisar",
                "body": "El documento adjunto contiene la información principal, pero el texto extraído puede ser insuficiente.",
                "sender": "cliente-adjunto@demo.invalid",
                "expected_department": None,
                "expected_category": "adjunto no interpretable",
                "expected_requires_review": True,
                "criticality": "high",
                "attachment_text": "[texto no disponible: documento escaneado]",
            },
        ]
    )
    return cases
