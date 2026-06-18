from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('compile/', views.compile_code, name='compile_code'),
]