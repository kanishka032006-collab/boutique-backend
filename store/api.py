from rest_framework.views import exception_handler as default_exception_handler


def exception_handler(exc, context):
    response = default_exception_handler(exc, context)
    if response is None:
        return response
    if isinstance(response.data, dict) and "detail" in response.data:
        response.data = {"error": str(response.data["detail"])}
    elif isinstance(response.data, dict):
        response.data = {"error": "Please check the submitted information.", "details": response.data}
    else:
        response.data = {"error": "Please check the submitted information.", "details": response.data}
    return response
